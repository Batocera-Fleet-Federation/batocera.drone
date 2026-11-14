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
    from ..common.settings import Settings
    from ..storage.state_store import load_payload, save_payload
    from . import network_es_overlay
except ImportError:  # pragma: no cover - direct script execution fallback
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
        return Path(override)
    return Path("/var/run/drone-nfs-export")


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


def cleanup_legacy_network_references(settings: Settings) -> None:
    """Unmount active legacy network shares, remove symlinks/mount points, and
    revoke legacy NFS exports on startup."""
    database = settings.state_database_file
    umount_bin = shutil.which("umount") or "umount"
    share_dir = _share_root(settings)

    share_state = load_payload(database, _NETWORK_SHARE_STATE_FILE, None)
    peers = share_state.get("peers") if isinstance(share_state, dict) else None
    if isinstance(peers, dict) and peers:
        previous_es_setting = share_state.get("es_parse_gamelist_only_before")
        try:
            network_es_overlay.remove(
                settings,
                previous_es_setting if isinstance(previous_es_setting, dict) else None,
            )
        except Exception:
            pass

        mount_points: list[Path] = [share_dir]
        for peer_id, peer_entry in list(peers.items()):
            if not isinstance(peer_entry, dict):
                continue
            raw_mp = str(peer_entry.get("mount_point") or "").strip()
            mp = Path(raw_mp) if raw_mp else (share_dir / str(peer_id).replace(":", "_"))
            mount_points.append(mp)
            _cleanup_rom_and_bios_links(settings, peer_entry, mount_points)
            if _unmount_path(mp, umount_bin) and mp.exists():
                shutil.rmtree(mp, ignore_errors=True)

        save_payload(database, _NETWORK_SHARE_STATE_FILE, {"schema_version": 1, "peers": {}})
        save_payload(database, _NETWORK_REFERENCE_SELECTION_FILE, {})
        try:
            (settings.userdata_root / "system" / _NETWORK_SHARE_STATE_FILE).unlink(missing_ok=True)
        except OSError:
            pass
    elif network_es_overlay.overlay_path(settings).exists():
        try:
            network_es_overlay.remove(settings, None)
        except Exception:
            pass

    export_state = load_payload(database, _NFS_EXPORT_STATE_FILE, None)
    export_peers = export_state.get("peers") if isinstance(export_state, dict) else None
    if isinstance(export_peers, dict) and export_peers:
        exportfs_bin = shutil.which("exportfs") or "exportfs"
        export_root = _export_root()
        for peer_entry in list(export_peers.values()):
            if not isinstance(peer_entry, dict):
                continue
            for raw_addr in peer_entry.get("addresses") or []:
                address = str(raw_addr or "").strip()
                if not address:
                    continue
                for export_path in (export_root / "roms", export_root / "bios", export_root):
                    try:
                        subprocess.run(
                            [exportfs_bin, "-u", f"{address}:{export_path}"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            check=False,
                        )
                    except Exception:
                        pass

        for child in (export_root / "roms", export_root / "bios"):
            _unmount_path(child, umount_bin)

        save_payload(database, _NFS_EXPORT_STATE_FILE, {"schema_version": 1, "peers": {}})
        try:
            (settings.userdata_root / "system" / _NFS_EXPORT_STATE_FILE).unlink(missing_ok=True)
        except OSError:
            pass
