"""Serve this Drone's local ROM and BIOS libraries to paired peers over NFSv4.

Batocera already ships and starts the kernel NFS service on supported builds,
but does not export any directories by default.  Drone adds only a small,
private pseudo-root containing resolved ROM-system bind mounts plus a BIOS
bind mount.  Resolving each ROM system on the source is important: Batocera
commonly represents systems as absolute symlinks into an external drive, and
NFS would otherwise hand that source-only link text to the client.  Drone
never exports all of ``/userdata`` and never edits Batocera's own exports file.

Authorizations are keyed by paired Drone id and exact, trusted IP addresses.
They are persisted in Drone's SQLite state store and replayed after a service
or machine restart.  Every export is read-only; NFS is only the byte transport
and the client keeps saves/configuration on its own disk.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from ..common.install_paths import drone_install_root as _drone_install_root
    from ..common.network_references import is_network_reference, network_reference_root
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..transfer import local_network as _local_network
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.install_paths import drone_install_root as _drone_install_root  # type: ignore
    from common.network_references import is_network_reference, network_reference_root  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from transfer import local_network as _local_network  # type: ignore


NFS_EXPORT_STATE_NAMESPACE = "nfs_export_manager.json"
NFS_EXPORT_STATE_SCHEMA_VERSION = 1
NFS_EXPORT_PORT = 2049
NFS_EXPORT_COMMAND_TIMEOUT_SECONDS = float(os.environ.get("DRONE_NFS_EXPORT_COMMAND_TIMEOUT_SECONDS", "10"))
NFS_EXPORT_BASE_OPTIONS = "ro,sync,no_subtree_check,root_squash"
NFS_EXPORT_ROOT_OPTIONS = f"{NFS_EXPORT_BASE_OPTIONS},fsid=0,crossmnt"
# Both bind mounts normally originate on the same /userdata filesystem. Give
# them distinct stable UUID-style fsids instead of relying on implicit
# crossmnt ids derived from the same backing device.
NFS_EXPORT_CHILD_FSIDS = {
    "roms": "7b0bfa2a7e1a5df3a7ad8f59386f80a1",
    "bios": "fda715050a2e55ac96b5535d59829c70",
}
NFS_EXPORT_ROM_OPTIONS = (
    f"{NFS_EXPORT_BASE_OPTIONS},fsid={NFS_EXPORT_CHILD_FSIDS['roms']},crossmnt"
)

_STATE_LOCK = threading.RLock()
_PRIVATE_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def export_root(settings: Settings) -> Path:
    configured = str(os.environ.get("DRONE_NFS_EXPORT_ROOT") or "").strip()
    if configured:
        return Path(configured).resolve()
    return _drone_install_root() / "nfs-export"


def _load_state(settings: Settings) -> dict:
    with _STATE_LOCK:
        stored = _load_state_payload(_state_database_path(settings.userdata_root), NFS_EXPORT_STATE_NAMESPACE, {})
        stored = stored if isinstance(stored, dict) else {}
        peers = stored.get("peers")
        return {
            "schema_version": NFS_EXPORT_STATE_SCHEMA_VERSION,
            "peers": peers if isinstance(peers, dict) else {},
        }


def _save_state(settings: Settings, state: dict) -> None:
    with _STATE_LOCK:
        _save_state_payload(
            _state_database_path(settings.userdata_root),
            NFS_EXPORT_STATE_NAMESPACE,
            {
                "schema_version": NFS_EXPORT_STATE_SCHEMA_VERSION,
                "peers": state.get("peers") if isinstance(state.get("peers"), dict) else {},
            },
        )


def _find_command(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for parent in (Path("/usr/sbin"), Path("/sbin"), Path("/usr/bin"), Path("/bin")):
        candidate = parent / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _nfs_server_versions() -> list[str]:
    try:
        raw = Path("/proc/fs/nfsd/versions").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    enabled = {token[1:] for token in raw.split() if token.startswith("+")}
    return [version for version in ("4.2", "4.1", "4") if version in enabled]


def capabilities() -> dict:
    exportfs = _find_command("exportfs")
    mount = _find_command("mount")
    umount = _find_command("umount")
    versions = _nfs_server_versions()
    errors = []
    if not exportfs:
        errors.append("exportfs is not installed")
    if not mount or not umount:
        errors.append("mount/umount is not installed")
    if not versions:
        errors.append("the kernel NFSv4 server is not active")
    return {
        "available": not errors,
        "protocol": "nfs",
        "versions": versions,
        "preferred_version": versions[0] if versions else "",
        "port": NFS_EXPORT_PORT,
        "detail": "; ".join(errors),
    }


def _normalized_client_ipv4(value: object) -> Optional[str]:
    raw = str(value or "").strip().split("%", 1)[0]
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if address.version != 4 or address.is_loopback or address.is_multicast or address.is_unspecified:
        return None
    if not any(address in network for network in _PRIVATE_IPV4_NETWORKS):
        return None
    return str(address)


def _authorized_addresses(peer: dict, observed_address: object) -> list[str]:
    addresses = []
    for candidate in (observed_address, peer.get("tailnet_ip"), peer.get("source_ip")):
        normalized = _normalized_client_ipv4(candidate)
        if normalized and normalized not in addresses:
            addresses.append(normalized)
    return addresses


def _mounted_paths() -> set[str]:
    paths = set()
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return paths
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        paths.add(fields[4].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\"))
    return paths


def _is_mounted(path: Path) -> bool:
    return os.path.abspath(os.path.normpath(str(path))) in _mounted_paths()


def _path_depth(path: Path) -> int:
    return len(Path(os.path.abspath(os.path.normpath(str(path)))).parts)


def _mounted_descendants(path: Path) -> list[Path]:
    root = os.path.abspath(os.path.normpath(str(path)))
    prefix = root.rstrip(os.sep) + os.sep
    return sorted(
        (Path(candidate) for candidate in _mounted_paths() if candidate == root or candidate.startswith(prefix)),
        key=_path_depth,
        reverse=True,
    )


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=NFS_EXPORT_COMMAND_TIMEOUT_SECONDS,
    )


def _command_error(result: subprocess.CompletedProcess, fallback: str) -> str:
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    return detail[-1] if detail else fallback


def _unmount_path(path: Path, *, best_effort: bool = False) -> None:
    if not _is_mounted(path):
        return
    umount = _find_command("umount")
    if not umount:
        if best_effort:
            return
        raise RuntimeError("umount is not installed")
    try:
        result = _run([umount, str(path)])
        if result.returncode != 0 and _is_mounted(path):
            result = _run([umount, "-l", str(path)])
        if result.returncode != 0 and _is_mounted(path) and not best_effort:
            raise RuntimeError(_command_error(result, f"could not unmount {path}"))
    except (OSError, subprocess.SubprocessError) as error:
        if not best_effort:
            raise RuntimeError(f"could not unmount {path}: {error}") from error


def _same_directory(left: Path, right: Path) -> bool:
    try:
        left_stat = left.stat()
        right_stat = right.stat()
    except OSError:
        return False
    return (left_stat.st_dev, left_stat.st_ino) == (right_stat.st_dev, right_stat.st_ino)


def _ensure_bind_mount(source: Path, target: Path, *, mounted: Optional[bool] = None) -> None:
    if not source.is_dir():
        raise RuntimeError(f"NFS source directory does not exist: {source}")
    if target.is_symlink():
        raise RuntimeError(f"NFS export target must not be a symlink: {target}")
    target.mkdir(parents=True, exist_ok=True)
    target_is_mounted = _is_mounted(target) if mounted is None else mounted
    if target_is_mounted:
        if _same_directory(source, target):
            return
        _unmount_path(target)
    mount = _find_command("mount")
    if not mount:
        raise RuntimeError("mount is not installed")
    result = _run([mount, "--bind", str(source), str(target)])
    if result.returncode != 0:
        raise RuntimeError(_command_error(result, f"could not bind mount {source}"))


def _should_include_rom_entry(name: str) -> bool:
    lowered = str(name or "").strip().lower()
    return bool(lowered) and not (lowered.endswith(".old") or ".old." in lowered)


def _resolved_rom_sources(settings: Settings) -> dict[str, Path]:
    """Return the source-local directory for each shareable ROM system.

    The returned paths are fully resolved on the source Drone.  This converts
    entries such as ``/userdata/roms/snes -> /media/roms/...`` into the real
    directory that must be bind-mounted into the NFS namespace.  Drone-owned
    network references are deliberately excluded to avoid recursively
    re-exporting another peer's filesystem.
    """
    sources: dict[str, Path] = {}
    share_root = network_reference_root()
    try:
        entries = sorted(Path(settings.roms_root).iterdir(), key=lambda path: path.name.lower())
    except OSError as error:
        raise RuntimeError(f"could not inspect the ROM library: {error}") from error
    for entry in entries:
        if not _should_include_rom_entry(entry.name):
            continue
        if entry.is_symlink() and is_network_reference(entry, share_root):
            continue
        try:
            resolved = entry.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir():
            sources[entry.name] = resolved
    return sources


def _remove_empty_mountpoint(path: Path) -> None:
    try:
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError as error:
        raise RuntimeError(f"NFS export mountpoint is not empty: {path}") from error


def _ensure_resolved_rom_export(settings: Settings, root: Path) -> None:
    """Build ``roms/`` from per-system binds of fully resolved source paths."""
    roms_export = root / "roms"
    if roms_export.is_symlink():
        raise RuntimeError(f"NFS ROM export root must not be a symlink: {roms_export}")
    roms_export.mkdir(parents=True, exist_ok=True)

    # v0.1.134 and earlier bind-mounted the whole ROM root here. Remove that
    # legacy mount before creating owned, empty per-system mountpoints.
    mounted_targets = set(_mounted_descendants(roms_export))
    if roms_export in mounted_targets:
        for mounted in sorted(mounted_targets, key=_path_depth, reverse=True):
            _unmount_path(mounted)
        mounted_targets.clear()

    sources = _resolved_rom_sources(settings)
    desired_targets = {roms_export / name: source for name, source in sources.items()}

    # Reconcile stale or retargeted system mounts before adding current ones.
    for mounted in sorted(mounted_targets, key=_path_depth, reverse=True):
        source = desired_targets.get(mounted)
        if source is None or not _same_directory(source, mounted):
            _unmount_path(mounted)
            mounted_targets.discard(mounted)

    try:
        existing_entries = list(roms_export.iterdir())
    except OSError as error:
        raise RuntimeError(f"could not inspect the NFS ROM export: {error}") from error
    for entry in existing_entries:
        if entry not in desired_targets:
            if entry.is_symlink() or not entry.is_dir():
                raise RuntimeError(f"unexpected entry in Drone-owned NFS ROM export: {entry}")
            _remove_empty_mountpoint(entry)

    for target, source in desired_targets.items():
        _ensure_bind_mount(source, target, mounted=target in mounted_targets)


def _ensure_export_root(settings: Settings) -> Path:
    root = export_root(settings)
    if root.is_symlink():
        raise RuntimeError(f"NFS export root must not be a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True)
    _ensure_resolved_rom_export(settings, root)
    _ensure_bind_mount(Path(settings.bios_root), root / "bios")
    return root


def _export_specs(settings: Settings) -> list[tuple[Path, str]]:
    root = export_root(settings)
    return [
        (root, NFS_EXPORT_ROOT_OPTIONS),
        (root / "roms", NFS_EXPORT_ROM_OPTIONS),
        (root / "bios", f"{NFS_EXPORT_BASE_OPTIONS},fsid={NFS_EXPORT_CHILD_FSIDS['bios']}"),
    ]


def _unexport_path(exportfs: str, address: str, path: Path) -> Optional[str]:
    try:
        result = _run([exportfs, "-u", f"{address}:{path}"])
    except (OSError, subprocess.SubprocessError) as error:
        return str(error)
    if result.returncode != 0:
        return _command_error(result, f"could not revoke NFS client {address} from {path}")
    return None


def _export_client(settings: Settings, address: str) -> None:
    exportfs = _find_command("exportfs")
    if not exportfs:
        raise RuntimeError("exportfs is not installed")
    exported = []
    for path, options in _export_specs(settings):
        result = _run([exportfs, "-i", "-o", options, f"{address}:{path}"])
        if result.returncode == 0:
            exported.append(path)
            continue
        for rollback_path in reversed(exported):
            _unexport_path(exportfs, address, rollback_path)
        raise RuntimeError(_command_error(result, f"could not authorize NFS client {address} for {path}"))


def _unexport_client(settings: Settings, address: str) -> Optional[str]:
    exportfs = _find_command("exportfs")
    if not exportfs:
        return "exportfs is not installed"
    errors = [
        error
        for path, _options in reversed(_export_specs(settings))
        if (error := _unexport_path(exportfs, address, path))
    ]
    return errors[0] if errors else None


def _cleanup_bind_mounts(settings: Settings) -> None:
    root = export_root(settings)
    roms_export = root / "roms"
    for target in [*_mounted_descendants(roms_export), root / "bios"]:
        _unmount_path(target, best_effort=True)


def authorize_peer(settings: Settings, peer_id: str, observed_address: object = "") -> dict:
    """Authorize one paired Drone and return its mount contract."""
    peer_id = str(peer_id or "").strip()
    peer = _local_network.get_paired_peer(settings, peer_id)
    if not peer:
        raise ValueError("not a paired peer")
    addresses = _authorized_addresses(peer, observed_address)
    if not addresses:
        raise ValueError("paired peer has no trusted LAN or Tailscale IPv4 address")
    capability = capabilities()
    if not capability["available"]:
        raise RuntimeError(capability["detail"] or "NFSv4 is unavailable")

    with _STATE_LOCK:
        state = _load_state(settings)
        prior = state["peers"].get(peer_id) if isinstance(state["peers"].get(peer_id), dict) else {}
        prior_addresses = {
            str(address) for address in prior.get("addresses") or []
            if _normalized_client_ipv4(address)
        }
        shared_elsewhere = {
            str(address)
            for other_id, other in state["peers"].items()
            if other_id != peer_id and isinstance(other, dict)
            for address in other.get("addresses") or []
        }
        record = {
            **prior,
            "peer_id": peer_id,
            "peer_name": str(peer.get("name") or peer.get("hostname") or peer_id),
            "addresses": addresses,
            "status": "authorizing",
            "status_detail": "Preparing read-only NFSv4 export",
            "updated_at": _now_iso(),
        }
        state["peers"][peer_id] = record
        _save_state(settings, state)
        try:
            _ensure_export_root(settings)
            for address in addresses:
                _export_client(settings, address)
            retired = sorted(prior_addresses - set(addresses) - shared_elsewhere)
            retired_errors = [
                f"{address}: {error}"
                for address in retired
                if (error := _unexport_client(settings, address))
            ]
            if retired_errors:
                # Keep failed retired addresses in state so a later refresh,
                # unpair, or uninstall can retry their removal rather than
                # losing ownership of a stale authorization.
                record["addresses"] = addresses + [
                    address for address in retired
                    if any(item.startswith(f"{address}: ") for item in retired_errors)
                ]
                raise RuntimeError(f"could not retire an old NFS authorization: {retired_errors[0]}")
        except Exception as error:
            record.update(status="error", status_detail=str(error), updated_at=_now_iso())
            _save_state(settings, state)
            raise
        record.update(status="active", status_detail="", updated_at=_now_iso())
        _save_state(settings, state)

    return {
        **capability,
        "export_path": "/",
        "authorized_addresses": addresses,
        "peer_id": peer_id,
    }


def revoke_peer(settings: Settings, peer_id: str) -> dict:
    peer_id = str(peer_id or "").strip()
    with _STATE_LOCK:
        state = _load_state(settings)
        record = state["peers"].get(peer_id)
        if not isinstance(record, dict):
            return {"status": "not_found", "peer_id": peer_id}
        shared_elsewhere = {
            str(address)
            for other_id, other in state["peers"].items()
            if other_id != peer_id and isinstance(other, dict)
            for address in other.get("addresses") or []
        }
        errors = []
        for address in record.get("addresses") or []:
            if str(address) in shared_elsewhere:
                continue
            error = _unexport_client(settings, str(address))
            if error:
                errors.append(error)
        if errors:
            record.update(status="error", status_detail=errors[0], updated_at=_now_iso())
            _save_state(settings, state)
            return {"status": "error", "peer_id": peer_id, "status_detail": errors[0]}
        state["peers"].pop(peer_id, None)
        _save_state(settings, state)
        if not state["peers"]:
            _cleanup_bind_mounts(settings)
        return {"status": "revoked", "peer_id": peer_id}


def restore_exports(settings: Settings) -> None:
    """Replay persistent authorizations without delaying Drone HTTP startup."""
    with _STATE_LOCK:
        state = _load_state(settings)
        if not state["peers"]:
            return
        paired = {
            str(peer.get("drone_id") or ""): peer
            for peer in _local_network.paired_peers(settings)
            if str(peer.get("drone_id") or "").strip()
        }
        for peer_id in list(state["peers"]):
            if peer_id in paired:
                continue
            record = state["peers"].pop(peer_id)
            shared_elsewhere = {
                str(address)
                for other in state["peers"].values()
                if isinstance(other, dict)
                for address in other.get("addresses") or []
            }
            errors = []
            for address in record.get("addresses") or []:
                if str(address) in shared_elsewhere:
                    continue
                error = _unexport_client(settings, str(address))
                if error:
                    errors.append(error)
            if errors:
                # Retain ownership metadata so a later restart or uninstall
                # can retry; never silently orphan a live exportfs entry.
                record.update(status="error", status_detail=errors[0], updated_at=_now_iso())
                state["peers"][peer_id] = record
        paired_records = {
            peer_id: record
            for peer_id, record in state["peers"].items()
            if peer_id in paired and isinstance(record, dict)
        }
        if not paired_records:
            _save_state(settings, state)
            # Even if exportfs cleanup failed, removing the child bind mounts
            # prevents the stale client from reaching ROM/BIOS data while the
            # retained record remains available for a later cleanup retry.
            _cleanup_bind_mounts(settings)
            return
        try:
            _ensure_export_root(settings)
        except Exception as error:
            for record in state["peers"].values():
                if isinstance(record, dict):
                    record.update(status="error", status_detail=str(error), updated_at=_now_iso())
            _save_state(settings, state)
            return
        for peer_id, record in paired_records.items():
            try:
                for address in record.get("addresses") or []:
                    _export_client(settings, str(address))
                record.update(status="active", status_detail="", updated_at=_now_iso())
            except Exception as error:
                record.update(status="error", status_detail=str(error), updated_at=_now_iso())
        _save_state(settings, state)


def cleanup_all_exports(settings: Settings) -> dict:
    """Remove only Drone-owned exports and bind mounts, for uninstall."""
    with _STATE_LOCK:
        state = _load_state(settings)
        addresses = {
            str(address)
            for record in state["peers"].values()
            if isinstance(record, dict)
            for address in record.get("addresses") or []
        }
        errors = [error for address in addresses if (error := _unexport_client(settings, address))]
        if not errors:
            state["peers"] = {}
            _save_state(settings, state)
        _cleanup_bind_mounts(settings)
        return {"status": "error" if errors else "cleaned", "errors": errors}


def main(argv: Optional[list[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args != ["cleanup"]:
        print("Usage: python3 -m app.device.nfs_export_manager cleanup", file=sys.stderr)
        return 2
    result = cleanup_all_exports(Settings.from_env())
    if result["status"] != "cleaned":
        for error in result.get("errors") or []:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - operational entrypoint
    raise SystemExit(main())
