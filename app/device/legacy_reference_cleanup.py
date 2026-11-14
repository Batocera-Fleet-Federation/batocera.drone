"""One-time upgrade cleanup for removed Reference ROMs / NFS network shares.

Drones upgrading from earlier releases may have active network-share mounts,
ROM/BIOS symlinks, EmulationStation overlay files, or source-side NFS exports
recorded in state_store. On server startup, this helper unmounts any active
shares, removes symlinks and mount points, revokes peer NFS exports, and
clears the legacy state entries.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

try:
    from ..common.install_paths import drone_install_root
    from ..common.settings import Settings
    from ..storage.state_store import load_payload, save_payload
    from . import network_es_overlay
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.install_paths import drone_install_root  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage.state_store import load_payload, save_payload  # type: ignore
    from device import network_es_overlay  # type: ignore


_NETWORK_SHARE_STATE_FILE = "network_share_manager.json"
_NFS_EXPORT_STATE_FILE = "nfs_export_manager.json"
_NETWORK_REFERENCE_SELECTION_FILE = "network_reference_selection.json"


def _share_root(settings: Settings) -> Path:
    override = os.environ.get("DRONE_NETWORK_SHARE_DIR", "").strip()
    if override:
        return Path(override)
    return settings.userdata_root / "system" / "drone-network-shares"


def _export_root() -> Path:
    override = os.environ.get("DRONE_NFS_EXPORT_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    # The historical exporter's default tree (<install root>/nfs-export).
    return drone_install_root() / "nfs-export"


def _mounted_descendants(root: Path) -> list[Path]:
    """Bind mounts under the old export tree, deepest first."""
    base = os.path.abspath(os.path.normpath(str(root)))
    prefix = base.rstrip(os.sep) + os.sep
    found: set[str] = set()
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        mount_point = fields[4].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
        if mount_point == base or mount_point.startswith(prefix):
            found.add(mount_point)
    return [Path(item) for item in sorted(found, key=lambda value: len(Path(value).parts), reverse=True)]


def _unmount_path(mount_point: Path, umount_bin: str) -> bool:
    target = str(mount_point)
    if not os.path.ismount(target):
        return True
    try:
        subprocess.run(
            [umount_bin, target],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        pass
    if os.path.ismount(target):
        try:
            subprocess.run(
                [umount_bin, "-l", target],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception:
            pass
    return not os.path.ismount(target)


def _cleanup_rom_and_bios_links(settings: Settings, peer_entry: dict, mount_points: list[Path]) -> None:
    roms_root = settings.roms_root
    bios_root = settings.bios_root

    for sys_entry in peer_entry.get("systems") or []:
        if not isinstance(sys_entry, dict):
            continue
        system = str(sys_entry.get("system") or "").strip()
        if not system:
            continue
        link = roms_root / system
        if link.is_symlink():
            try:
                link.unlink()
            except OSError:
                pass
        backup = roms_root / f"{system}.old"
        if backup.exists() and not link.exists() and not link.is_symlink():
            try:
                backup.rename(link)
            except OSError:
                pass

    for bios_entry in peer_entry.get("bios") or []:
        if not isinstance(bios_entry, dict):
            continue
        rel = str(bios_entry.get("relative_path") or "").strip()
        if not rel:
            continue
        link = bios_root / rel
        if link.is_symlink():
            try:
                link.unlink()
            except OSError:
                pass

    # Also sweep any top-level symlinks under roms_root / bios_root that point into a share mount point.
    mount_prefixes = [str(mp) for mp in mount_points if str(mp)]
    for root_dir in (roms_root, bios_root):
        if not root_dir.is_dir():
            continue
        try:
            entries = list(root_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_symlink():
                continue
            try:
                raw_target = str(os.readlink(entry))
            except OSError:
                continue
            if any(raw_target == prefix or raw_target.startswith(prefix + os.sep) for prefix in mount_prefixes):
                try:
                    entry.unlink()
                except OSError:
                    pass


def _find_command(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for parent in (Path("/usr/sbin"), Path("/sbin"), Path("/usr/bin"), Path("/bin")):
        candidate = parent / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _run_ok(args: list[str]) -> bool:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
    except Exception:
        return False
    return getattr(result, "returncode", 1) == 0


def _address_export_present(exportfs_bin: str, address: str, paths: set[str]) -> bool | None:
    """True/False when a successful ``exportfs -s`` listing proves the grant is
    present/absent; None when the listing cannot be trusted."""
    try:
        result = subprocess.run(
            [exportfs_bin, "-s"], capture_output=True, text=True, timeout=10, check=False
        )
    except Exception:
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    for line in str(result.stdout or "").splitlines():
        fields = line.split()
        if len(fields) < 2:
            return None
        if fields[0] in paths and fields[1].split("(", 1)[0] == address:
            return True
    return False


def _revoke_address(exportfs_bin: str, address: str, export_root: Path) -> bool:
    """Revoke one client address from every legacy export path. True only when
    every path was unexported (or the kernel listing proves it already absent)."""
    paths = (export_root / "roms", export_root / "bios", export_root)
    all_ok = True
    for export_path in paths:
        if not _run_ok([exportfs_bin, "-u", f"{address}:{export_path}"]):
            all_ok = False
    if all_ok:
        return True
    return _address_export_present(exportfs_bin, address, {str(p) for p in paths}) is False


def _cleanup_network_shares(settings: Settings, database, umount_bin: str) -> None:
    share_dir = _share_root(settings)
    share_state = load_payload(database, _NETWORK_SHARE_STATE_FILE, None)
    peers = share_state.get("peers") if isinstance(share_state, dict) else None
    if not (isinstance(peers, dict) and peers):
        if network_es_overlay.overlay_path(settings).exists():
            try:
                network_es_overlay.remove(settings, None)
            except Exception:
                pass
        return

    previous_es_setting = share_state.get("es_parse_gamelist_only_before")
    try:
        overlay_errors = network_es_overlay.remove(
            settings,
            previous_es_setting if isinstance(previous_es_setting, dict) else None,
        )
    except Exception:
        overlay_errors = ["overlay removal failed"]

    mount_points: list[Path] = [share_dir]
    remaining: dict = {}
    for peer_id, peer_entry in list(peers.items()):
        if not isinstance(peer_entry, dict):
            continue
        raw_mp = str(peer_entry.get("mount_point") or "").strip()
        mp = Path(raw_mp) if raw_mp else (share_dir / str(peer_id).replace(":", "_"))
        mount_points.append(mp)
        _cleanup_rom_and_bios_links(settings, peer_entry, mount_points)
        if _unmount_path(mp, umount_bin):
            if mp.exists():
                shutil.rmtree(mp, ignore_errors=True)
        else:
            # Still attached: keep the record (and its mount_point) so the
            # next start retries instead of orphaning a stale mount.
            remaining[peer_id] = peer_entry

    if remaining or overlay_errors:
        retained = {"schema_version": 1, "peers": remaining}
        if overlay_errors and isinstance(previous_es_setting, dict):
            retained["es_parse_gamelist_only_before"] = previous_es_setting
        if not remaining:
            # Overlay-only failure: keep a placeholder entry so retry still runs.
            retained["peers"] = {
                str(pid): entry for pid, entry in peers.items() if isinstance(entry, dict)
            }
        save_payload(database, _NETWORK_SHARE_STATE_FILE, retained)
        return

    save_payload(database, _NETWORK_SHARE_STATE_FILE, {"schema_version": 1, "peers": {}})
    save_payload(database, _NETWORK_REFERENCE_SELECTION_FILE, {})
    try:
        (settings.userdata_root / "system" / _NETWORK_SHARE_STATE_FILE).unlink(missing_ok=True)
    except OSError:
        pass


def _cleanup_nfs_exports(settings: Settings, database, umount_bin: str) -> None:
    export_state = load_payload(database, _NFS_EXPORT_STATE_FILE, None)
    if not isinstance(export_state, dict):
        return
    export_peers = export_state.get("peers")
    export_peers = export_peers if isinstance(export_peers, dict) else {}
    if not export_peers and not export_state.get("unmount_pending"):
        return

    export_root = _export_root()
    exportfs_bin = _find_command("exportfs")
    remaining: dict = {}
    for peer_id, peer_entry in list(export_peers.items()):
        if not isinstance(peer_entry, dict):
            continue
        failed_addresses = []
        for raw_addr in peer_entry.get("addresses") or []:
            address = str(raw_addr or "").strip()
            if not address:
                continue
            # No exportfs binary means the kernel cannot hold any export.
            if exportfs_bin and not _revoke_address(exportfs_bin, address, export_root):
                failed_addresses.append(address)
        if failed_addresses:
            remaining[peer_id] = {**peer_entry, "addresses": failed_addresses}

    mounted = _mounted_descendants(export_root)
    unmount_ok = True
    for child in mounted or [export_root / "roms", export_root / "bios"]:
        if not _unmount_path(child, umount_bin):
            unmount_ok = False

    if remaining or not unmount_ok:
        save_payload(
            database,
            _NFS_EXPORT_STATE_FILE,
            {"schema_version": 1, "peers": remaining, "unmount_pending": not unmount_ok},
        )
        return

    save_payload(database, _NFS_EXPORT_STATE_FILE, {"schema_version": 1, "peers": {}})
    try:
        (settings.userdata_root / "system" / _NFS_EXPORT_STATE_FILE).unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_legacy_network_references(settings: Settings) -> None:
    """Unmount active legacy network shares, remove symlinks/mount points, and
    revoke legacy NFS exports on startup.

    State records are erased only for work that verifiably completed; anything
    that failed (busy mount, failing ``exportfs -u``) stays recorded so the next
    startup retries it.
    """
    database = settings.state_database_file
    umount_bin = shutil.which("umount") or "umount"
    _cleanup_network_shares(settings, database, umount_bin)
    _cleanup_nfs_exports(settings, database, umount_bin)
