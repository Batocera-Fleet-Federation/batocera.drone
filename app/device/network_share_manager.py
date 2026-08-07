"""Reference a paired peer's whole ROM library and BIOS folder over the network.

New mounts negotiate a private, read-only NFSv4 export through the paired mTLS
API and prefer it over both LAN and Tailscale. Older peers and NFS failures
fall back to Batocera's stock ``//<peer>/share`` SMB export. Both transports
present the same ``roms/`` + ``bios/`` layout at the same local mount point,
so reconciliation and recovery remain transport-independent.

**ROMs** are reconciled directory-by-directory (one system = one directory):
either symlink a system straight in (no local games for it yet), or, if a
local folder with real content already exists, rename it out of the way to
``<system>.old`` first and symlink over it -- never deleting anything.
**BIOS** files are reconciled file-by-file instead (BIOS is a flat pile of
individual dependency files, some nested under a per-emulator subfolder, not
a browsable catalog the way ROM systems are). Existing local BIOS always win;
only missing files are supplied by read-only network symlinks.

``.old`` is not a new convention: ``RomRepository.should_include_system``
excludes renamed-aside folders from Drone's own system list (reused here for
ROMs). It is also the standard upstream
Batocera/EmulationStation trick for "keep on disk, don't show" -- reusing them
lets Batocera's native EmulationStation discover the remote system links. The
Drone metadata scanner deliberately excludes those links so routine scans and
peer-health summaries never walk a latency-sensitive network filesystem.

Every rename this module performs is recorded (which peer, which system/file,
the exact original name) so disabling a reference can precisely reverse only
what this module itself did -- never guessing from a bare ``.old`` suffix,
which could belong to something else entirely.

Structurally mirrors ``vpn_manager.py`` on purpose: Drone already runs as root
(see ``service_bootstrap.sh``), so mounting is a direct ``subprocess`` call,
no privilege-escalation dance. State lives in the same small JSON-blob-in-
SQLite pattern (``storage/state_store.py``) VPN uses for its own config,
rather than a new dedicated table -- this is feature configuration for a
handful of peers, not a large scannable inventory like ROMs/saves. Mounts are
read-only + ``soft`` (bounded failure instead of hanging
Drone's own ROM-scanning poller thread if the source peer goes dark
mid-session -- see the module docstring note in ``roms/rom_scanner.py`` for
why an unbounded hang there would be worse than one peer's share going stale).
Reconnects on every Drone service startup and self-heals on a background
watchdog thread, exactly like VPN's connect-on-boot + self-heal -- no fstab or
systemd unit is ever touched.
"""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

try:
    from ..common.install_paths import drone_install_root as _drone_install_root
    from ..common.network_references import is_network_reference as _is_network_reference
    from ..common.network_references import lexical_symlink_target as _lexical_symlink_target
    from ..common.network_references import symlink_points_to as _symlink_points_to
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..transfer import local_network as _local_network
    from ..transfer.peer_connectivity import _peer_get_json_for_peer, _peer_post_json_for_peer
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.install_paths import drone_install_root as _drone_install_root  # type: ignore
    from common.network_references import is_network_reference as _is_network_reference  # type: ignore
    from common.network_references import lexical_symlink_target as _lexical_symlink_target  # type: ignore
    from common.network_references import symlink_points_to as _symlink_points_to  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.peer_connectivity import _peer_get_json_for_peer, _peer_post_json_for_peer  # type: ignore

NETWORK_SHARE_STATE_NAMESPACE = "network_share_manager.json"
NETWORK_SHARE_STATUSES = ("mounted", "peer_unreachable", "error", "pending", "enabling", "detaching")
NETWORK_SHARE_OLD_SUFFIX = ".old"
NETWORK_SHARE_LAYOUT_SCHEMA_VERSION = 2
NETWORK_SHARE_STATE_SCHEMA_VERSION = 3

NETWORK_SHARE_MOUNT_TIMEOUT_SECONDS = float(os.environ.get("DRONE_NETWORK_SHARE_MOUNT_TIMEOUT_SECONDS", "20"))
NETWORK_SHARE_UMOUNT_TIMEOUT_SECONDS = float(os.environ.get("DRONE_NETWORK_SHARE_UMOUNT_TIMEOUT_SECONDS", "10"))
# How often the watchdog re-probes every enabled share. Deliberately simpler
# than VPN's rate-limited self-heal (no attempts-per-window backoff): a failed
# network-filesystem mount attempt is cheap and bounded by subprocess timeouts
# plus the transport's `soft` mount option,
# unlike VPN's remote-provider reconnect which can be a slow/costly handshake.
NETWORK_SHARE_WATCHDOG_INTERVAL_SECONDS = float(os.environ.get("DRONE_NETWORK_SHARE_WATCHDOG_INTERVAL_SECONDS", "60"))
NETWORK_SHARE_ATTRIBUTE_CACHE_SECONDS = max(1, int(os.environ.get("DRONE_NETWORK_SHARE_ACTIMEO_SECONDS", "30")))
NETWORK_SHARE_PEER_SUMMARY_TIMEOUT_SECONDS = float(os.environ.get("DRONE_NETWORK_SHARE_PEER_SUMMARY_TIMEOUT_SECONDS", "6"))
NETWORK_SHARE_PEER_BIOS_INVENTORY_TIMEOUT_SECONDS = float(os.environ.get("DRONE_NETWORK_SHARE_PEER_BIOS_INVENTORY_TIMEOUT_SECONDS", "15"))
NETWORK_SHARE_NFS_NEGOTIATION_TIMEOUT_SECONDS = float(os.environ.get("DRONE_NETWORK_SHARE_NFS_NEGOTIATION_TIMEOUT_SECONDS", "8"))
NETWORK_SHARE_NFS_PORT = 2049
NETWORK_SHARE_NFS_TIMEOUT_TENTHS = max(1, int(os.environ.get("DRONE_NETWORK_SHARE_NFS_TIMEO_TENTHS", "50")))
NETWORK_SHARE_NFS_RETRANS = max(1, int(os.environ.get("DRONE_NETWORK_SHARE_NFS_RETRANS", "2")))

_STATE_MIGRATION_LOCK = threading.Lock()
_STATE_LOCK = threading.RLock()
_PEER_OPERATION_LOCKS_GUARD = threading.Lock()
_PEER_OPERATION_LOCKS = {}
_BACKGROUND_JOBS_LOCK = threading.Lock()
_ACTIVE_DISABLE_JOBS = set()
_ACTIVE_BIOS_JOBS = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _operation_key(settings: Settings, peer_id: str) -> str:
    return f"{_state_database_path(settings.userdata_root)}::{str(peer_id or '').strip()}"


def _peer_operation_lock(settings: Settings, peer_id: str) -> threading.RLock:
    """Serialize every lifecycle mutation for one peer.

    HTTP requests, boot replay, the watchdog, and background BIOS work all run
    on different threads.  Without this lock, an enable could recreate links
    while detach was removing them, or a watchdog could remount a share between
    detach's cleanup and final state deletion.
    """
    key = _operation_key(settings, peer_id)
    with _PEER_OPERATION_LOCKS_GUARD:
        lock = _PEER_OPERATION_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PEER_OPERATION_LOCKS[key] = lock
        return lock


def _bios_reconciliation_background_enabled() -> bool:
    return str(os.environ.get("DRONE_NETWORK_SHARE_BIOS_BACKGROUND", "1")).strip().lower() not in {
        "0", "false", "no", "off",
    }


def _should_include_entry(name: str) -> bool:
    # Mirrors RomRepository.should_include_system (app/drone_api.py) without
    # importing it -- used for both ROM system directory names and BIOS file
    # names, so a peer's own hidden/renamed-aside entries never get mirrored
    # back in as if they were real content.
    lowered = str(name or "").strip().lower()
    return bool(lowered) and not (lowered.endswith(NETWORK_SHARE_OLD_SUFFIX) or ".old." in lowered)


def _safe_dirname(value: str) -> str:
    # Peer ids look like MAC addresses (e.g. "58:47:ca:7e:38:57") -- not safe
    # to use as a path segment verbatim.
    return "".join(char if (char.isalnum() or char in "-_") else "_" for char in str(value or "").strip()) or "unknown"


def network_share_dir(settings: Settings) -> Path:
    # DRONE_NETWORK_SHARE_DIR is an escape hatch for tests/ops, matching the
    # DRONE_VPN_DIR-style override -- never exposed as a configurable field.
    configured = os.environ.get("DRONE_NETWORK_SHARE_DIR")
    if configured:
        return Path(configured).resolve()
    return _drone_install_root() / "network-shares"


def peer_mount_point(settings: Settings, peer_id: str) -> Path:
    return network_share_dir(settings) / _safe_dirname(peer_id)


# ------------------------------------------------------------------- state


def _load_state(settings: Settings) -> dict:
    with _STATE_LOCK:
        stored = _load_state_payload(_state_database_path(settings.userdata_root), NETWORK_SHARE_STATE_NAMESPACE, {})
        stored = stored if isinstance(stored, dict) else {}
        peers = stored.get("peers")
        try:
            schema_version = int(stored.get("schema_version") or 1)
        except (TypeError, ValueError):
            schema_version = 1
        return {"schema_version": schema_version, "peers": peers if isinstance(peers, dict) else {}}


def _save_state(settings: Settings, state: dict) -> None:
    with _STATE_LOCK:
        payload = {
            "schema_version": int(state.get("schema_version") or NETWORK_SHARE_STATE_SCHEMA_VERSION),
            "peers": state.get("peers") if isinstance(state.get("peers"), dict) else {},
        }
        _save_state_payload(_state_database_path(settings.userdata_root), NETWORK_SHARE_STATE_NAMESPACE, payload)


def _get_peer_record(settings: Settings, peer_id: str) -> Optional[dict]:
    return _load_state(settings)["peers"].get(str(peer_id or "").strip())


def _upsert_peer_record(settings: Settings, peer_id: str, **updates) -> dict:
    with _STATE_LOCK:
        state = _load_state(settings)
        peer_id = str(peer_id or "").strip()
        record = state["peers"].get(peer_id) or {
            "peer_id": peer_id, "peer_name": "", "tailnet_ip": "", "mount_point": "",
            "enabled": True, "status": "pending", "status_detail": "",
            "systems": [], "bios": [], "bios_status": "pending",
            "protocol": "", "nfs_version": "", "export_path": "",
            "transport_fallback_detail": "",
            "created_at": _now_iso(), "last_checked_at": None,
        }
        record.update(updates)
        record["updated_at"] = _now_iso()
        state["peers"][peer_id] = record
        _save_state(settings, state)
        return record


def _delete_peer_record(settings: Settings, peer_id: str) -> None:
    with _STATE_LOCK:
        state = _load_state(settings)
        state["peers"].pop(str(peer_id or "").strip(), None)
        _save_state(settings, state)


def list_shares(settings: Settings) -> List[dict]:
    return sorted(_load_state(settings)["peers"].values(), key=lambda row: str(row.get("peer_name") or row.get("peer_id") or "").lower())


def get_share(settings: Settings, peer_id: str) -> Optional[dict]:
    return _get_peer_record(settings, peer_id)


# ---------------------------------------------------------------- resolve


def resolve_peer_target(settings: Settings, peer_id: str) -> dict:
    """Resolve trusted LAN-first mount candidates for a paired peer.

    The peer record, not the browser payload, remains the sole source of every
    address.  A same-LAN address avoids routing thousands of filesystem
    metadata operations through the mesh; the tailnet address remains the
    cross-network fallback.
    """
    peer_id = str(peer_id or "").strip()
    if not peer_id:
        raise ValueError("peer_id is required")
    peer = _local_network.get_paired_peer(settings, peer_id)
    if not peer:
        raise ValueError("not a paired peer")
    addresses: List[str] = []

    def add_address(value: object) -> None:
        candidate = str(value or "").strip().strip("[]")
        if candidate and candidate not in addresses:
            addresses.append(candidate)

    for key in ("source_ip", "local_ip"):
        add_address(peer.get(key))
    for key in ("reachable_url", "advertised_reachable_url"):
        raw_url = str(peer.get(key) or "").strip()
        if not raw_url:
            continue
        try:
            add_address(urlparse(raw_url if "://" in raw_url else f"//{raw_url}").hostname)
        except ValueError:
            continue
    tailnet_ip = str(peer.get("tailnet_ip") or "").strip()
    add_address(tailnet_ip)
    if not addresses:
        raise ValueError("peer has no known LAN or Tailscale address")
    return {
        "peer_id": peer_id,
        "peer_name": str(peer.get("name") or peer.get("hostname") or peer_id),
        "tailnet_ip": tailnet_ip,
        "addresses": addresses,
        "peer": peer,
    }


# ------------------------------------------------------------------ mount


def _is_mounted(mount_point: Path) -> bool:
    # Reading mountinfo is lexical and never asks the mounted filesystem to
    # stat itself.  os.path.ismount() can enter an uninterruptible CIFS wait
    # precisely when detach most needs to remain responsive.
    try:
        expected = os.path.abspath(os.path.normpath(str(mount_point)))
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split()
            if len(fields) < 5:
                continue
            mounted_at = fields[4].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
            if mounted_at == expected:
                return True
        if sys.platform.startswith("linux"):
            return False
    except OSError:
        pass
    try:
        return os.path.ismount(str(mount_point))
    except OSError:
        return False


def _tcp_port_open(address: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((address, int(port)), timeout=max(0.1, timeout)):
            return True
    except OSError:
        return False


def _smb_port_open(address: str, timeout: float = 1.0) -> bool:
    """Cheaply reject an unreachable candidate before invoking mount.cifs."""
    return _tcp_port_open(address, 445, timeout)


def _mount(addresses, mount_point: Path) -> dict:
    mount_point.mkdir(parents=True, exist_ok=True)
    if _is_mounted(mount_point):
        return {"status": "mounted", "address": ""}
    candidates = [str(addresses)] if isinstance(addresses, str) else [str(value) for value in addresses or []]
    candidates = list(dict.fromkeys(value.strip() for value in candidates if value.strip()))
    if not candidates:
        return {"status": "error", "detail": "peer has no SMB address"}
    errors = []
    per_candidate_timeout = max(3.0, NETWORK_SHARE_MOUNT_TIMEOUT_SECONDS / max(1, len(candidates)))
    for address in candidates:
        if len(candidates) > 1 and not _smb_port_open(address):
            errors.append(f"{address}: SMB port unreachable")
            continue
        try:
            result = subprocess.run(
                # The whole share, not just .../share/roms -- roms/ and bios/
                # are both subfolders of it, so one mount covers both.
                [
                    "mount", "-t", "cifs", f"//{address}/share", str(mount_point), "-o",
                    # Batocera's Samba export does not provide stable server
                    # inode numbers.  The kernel otherwise discovers that only
                    # after remote metadata access and retries with serverino
                    # disabled, which added about a minute on the affected box.
                    f"guest,ro,soft,noserverino,actimeo={NETWORK_SHARE_ATTRIBUTE_CACHE_SECONDS}",
                ],
                capture_output=True,
                text=True,
                timeout=per_candidate_timeout,
            )
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"{address}: failed to run mount: {error}")
            continue
        if result.returncode == 0:
            return {"status": "mounted", "address": address}
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        errors.append(f"{address}: {detail[-1] if detail else f'mount exited with status {result.returncode}'}")
    return {"status": "error", "detail": "; ".join(errors) or "SMB mount failed"}


def _mount_nfs(addresses, mount_point: Path, contract: dict) -> dict:
    """Mount a negotiated NFSv4 pseudo-root over LAN, then Tailscale."""
    mount_point.mkdir(parents=True, exist_ok=True)
    if _is_mounted(mount_point):
        return {"status": "mounted", "address": "", "protocol": "nfs"}
    candidates = [str(addresses)] if isinstance(addresses, str) else [str(value) for value in addresses or []]
    candidates = list(dict.fromkeys(value.strip() for value in candidates if value.strip()))
    if not candidates:
        return {"status": "error", "detail": "peer has no NFS address", "protocol": "nfs"}
    versions = contract.get("versions") if isinstance(contract.get("versions"), list) else []
    version = str(contract.get("preferred_version") or (versions[0] if versions else "4.2"))
    if version not in {"4", "4.1", "4.2"}:
        version = "4.2"
    export_path = str(contract.get("export_path") or "/").strip()
    if not export_path.startswith("/") or ".." in PurePosixPath(export_path).parts:
        return {"status": "error", "detail": "peer returned an invalid NFS export path", "protocol": "nfs"}
    errors = []
    per_candidate_timeout = max(3.0, NETWORK_SHARE_MOUNT_TIMEOUT_SECONDS / max(1, len(candidates)))
    options = (
        f"ro,soft,timeo={NETWORK_SHARE_NFS_TIMEOUT_TENTHS},retrans={NETWORK_SHARE_NFS_RETRANS},"
        f"proto=tcp,port={NETWORK_SHARE_NFS_PORT},vers={version},noatime,"
        f"actimeo={NETWORK_SHARE_ATTRIBUTE_CACHE_SECONDS}"
    )
    for address in candidates:
        if not _tcp_port_open(address, NETWORK_SHARE_NFS_PORT):
            errors.append(f"{address}: NFS port unreachable")
            continue
        host = f"[{address}]" if ":" in address and not address.startswith("[") else address
        try:
            result = subprocess.run(
                ["mount", "-t", "nfs", f"{host}:{export_path}", str(mount_point), "-o", options],
                capture_output=True,
                text=True,
                timeout=per_candidate_timeout,
            )
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"{address}: failed to run NFS mount: {error}")
            continue
        if result.returncode == 0:
            return {
                "status": "mounted",
                "address": address,
                "protocol": "nfs",
                "nfs_version": version,
                "export_path": export_path,
            }
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        errors.append(f"{address}: {detail[-1] if detail else f'NFS mount exited with status {result.returncode}'}")
    return {"status": "error", "detail": "; ".join(errors) or "NFS mount failed", "protocol": "nfs"}


def _network_share_protocol_preference() -> str:
    preference = str(os.environ.get("DRONE_NETWORK_SHARE_PROTOCOL") or "auto").strip().lower()
    return preference if preference in {"auto", "nfs", "smb"} else "auto"


def _negotiate_nfs(settings: Settings, target: dict) -> dict:
    peer = target.get("peer") if isinstance(target.get("peer"), dict) else None
    peer_id = str(target.get("peer_id") or "").strip()
    if not peer or not str(peer.get("certificate_path") or "").strip():
        return {"available": False, "detail": "peer does not advertise the paired NFS capability API"}
    started = time.monotonic()
    try:
        payload, api_address = _peer_post_json_for_peer(
            peer,
            "/v1/api/peer/network-share/nfs/authorize",
            {"protocol_version": 1},
            settings,
            peer_id=peer_id,
            timeout=NETWORK_SHARE_NFS_NEGOTIATION_TIMEOUT_SECONDS,
            overall_deadline=started + NETWORK_SHARE_NFS_NEGOTIATION_TIMEOUT_SECONDS,
        )
    except HTTPError as error:
        return {"available": False, "detail": f"peer NFS authorization returned HTTP {error.code}"}
    except (OSError, URLError, ssl.SSLError, ValueError) as error:
        return {"available": False, "detail": f"peer NFS authorization failed: {error}"}
    if payload.get("available") is not True or str(payload.get("protocol") or "") != "nfs":
        return {"available": False, "detail": str(payload.get("detail") or payload.get("error") or "peer NFS is unavailable")}
    return {**payload, "api_address": api_address}


def _revoke_nfs_export(settings: Settings, target: dict) -> None:
    peer = target.get("peer") if isinstance(target.get("peer"), dict) else None
    peer_id = str(target.get("peer_id") or "").strip()
    if not peer or not peer_id or not str(peer.get("certificate_path") or "").strip():
        return
    try:
        _peer_post_json_for_peer(
            peer,
            "/v1/api/peer/network-share/nfs/revoke",
            {"protocol_version": 1},
            settings,
            peer_id=peer_id,
            timeout=NETWORK_SHARE_NFS_NEGOTIATION_TIMEOUT_SECONDS,
            overall_deadline=time.monotonic() + NETWORK_SHARE_NFS_NEGOTIATION_TIMEOUT_SECONDS,
        )
    except Exception:
        # The source persists exact-peer authorization and removes it when the
        # peer is forgotten. A local detach must never fail because the source
        # is currently offline; a future authorization refresh replaces it.
        return


def _mount_preferred_transport(settings: Settings, target: dict, mount_point: Path) -> dict:
    preference = _network_share_protocol_preference()
    nfs_detail = ""
    if preference != "smb":
        contract = _negotiate_nfs(settings, target)
        if contract.get("available") is True:
            nfs_result = _mount_nfs(target.get("addresses") or [], mount_point, contract)
            if nfs_result.get("status") == "mounted":
                return nfs_result
            nfs_detail = str(nfs_result.get("detail") or "NFS mount failed")
            _unmount(mount_point, detach_immediately=True)
            _revoke_nfs_export(settings, target)
        else:
            nfs_detail = str(contract.get("detail") or "NFS unavailable")
        if preference == "nfs":
            return {"status": "error", "detail": nfs_detail, "protocol": "nfs"}

    smb_result = _mount(target.get("addresses") or [], mount_point)
    smb_result["protocol"] = "smb"
    if nfs_detail:
        smb_result["transport_fallback_detail"] = nfs_detail
        if smb_result.get("status") != "mounted":
            smb_result["detail"] = f"NFS: {nfs_detail}; SMB: {smb_result.get('detail') or 'mount failed'}"
    return smb_result


def _unmount(mount_point: Path, *, detach_immediately: bool = False) -> None:
    if not _is_mounted(mount_point):
        return
    try:
        # Detach uses a lazy unmount first: it removes the mount from this
        # namespace without waiting for an unhealthy SMB server or for an
        # emulator's stale file handle.  Kernel cleanup can finish later while
        # local directory restoration and the HTTP response proceed normally.
        first = ["umount", "-l", str(mount_point)] if detach_immediately else ["umount", str(mount_point)]
        result = subprocess.run(first, capture_output=True, text=True, timeout=NETWORK_SHARE_UMOUNT_TIMEOUT_SECONDS)
        if result.returncode != 0 and _is_mounted(mount_point):
            # Batocera's own network-share teardown falls back to a lazy
            # unmount the same way (board/batocera/fsoverlay S11share script).
            subprocess.run(["umount", "-l", str(mount_point)], capture_output=True, text=True, timeout=NETWORK_SHARE_UMOUNT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        pass


# ------------------------------------------------------------ system refs


def _apply_system_references(
    settings: Settings,
    mount_point: Path,
    existing_systems: List[dict],
    peer_system_names: Optional[List[str]] = None,
) -> List[dict]:
    """Reconcile local roms_root against everything the mount's roms/ export
    currently exposes. Idempotent -- safe to call again on every boot replay /
    watchdog pass without re-doing work that's already correctly in place."""
    roms_root = settings.roms_root
    peer_roms_dir = mount_point / "roms"
    existing_by_name = {str(row.get("system") or ""): row for row in existing_systems}
    if peer_system_names is not None:
        peer_systems = sorted({str(name).strip() for name in peer_system_names if _should_include_entry(name)}, key=str.lower)
    else:
        try:
            # scandir receives the directory-entry type from the server's readdir
            # response on normal servers, avoiding a separate Path.stat round
            # trip for every system on a high-latency connection.
            with os.scandir(peer_roms_dir) as entries:
                peer_systems = sorted(
                    entry.name for entry in entries
                    if _should_include_entry(entry.name) and entry.is_dir(follow_symlinks=False)
                )
        except OSError:
            return existing_systems  # mount unreadable right now -- leave prior state untouched, watchdog will retry

    results: List[dict] = []
    for system in peer_systems:
        prior = existing_by_name.get(system)
        local_path = roms_root / system
        target = peer_roms_dir / system
        old_path = roms_root / f"{system}{NETWORK_SHARE_OLD_SUFFIX}"

        if prior and prior.get("symlink_created") and _symlink_points_to(local_path, target):
            results.append(prior)
            continue

        # Recover an interrupted first enable whose symlink was created after
        # the local folder was renamed but before the completed manifest was
        # committed. Both checks are lexical/local; neither follows the mount.
        if _symlink_points_to(local_path, target):
            renamed_to = old_path.name if old_path.exists() else ""
            results.append({"system": system, "had_local_collision": bool(renamed_to), "renamed_to": renamed_to, "symlink_created": True, "skipped_reason": ""})
            continue

        if local_path.is_symlink() and _is_network_reference(local_path, network_share_dir(settings)):
            if not _is_network_reference(local_path, mount_point):
                results.append({"system": system, "had_local_collision": True, "renamed_to": "", "symlink_created": False, "skipped_reason": "skipped: system is already referenced from another peer"})
                continue
            try:
                local_path.unlink()
            except OSError as error:
                results.append({"system": system, "had_local_collision": False, "renamed_to": "", "symlink_created": False, "skipped_reason": f"stale network symlink could not be replaced: {error}"})
                continue

        if not local_path.exists() and not local_path.is_symlink():
            try:
                local_path.symlink_to(target, target_is_directory=True)
            except OSError as error:
                results.append({"system": system, "had_local_collision": False, "renamed_to": "", "symlink_created": False, "skipped_reason": f"symlink failed: {error}"})
                continue
            renamed_to = old_path.name if old_path.exists() else ""
            results.append({"system": system, "had_local_collision": bool(renamed_to), "renamed_to": renamed_to, "symlink_created": True, "skipped_reason": ""})
            continue

        if old_path.exists() or old_path.is_symlink():
            results.append({"system": system, "had_local_collision": True, "renamed_to": "", "symlink_created": False, "skipped_reason": f"skipped: {old_path.name} already exists"})
            continue
        try:
            local_path.rename(old_path)
        except OSError as error:
            results.append({"system": system, "had_local_collision": True, "renamed_to": "", "symlink_created": False, "skipped_reason": f"rename failed: {error}"})
            continue
        try:
            local_path.symlink_to(target, target_is_directory=True)
        except OSError as error:
            try:
                old_path.rename(local_path)
                renamed_to = ""
            except OSError:
                # Preserve enough state for disable/recovery to retry the
                # restore if the immediate rollback itself was blocked.
                renamed_to = old_path.name
            results.append({"system": system, "had_local_collision": True, "renamed_to": renamed_to, "symlink_created": False, "skipped_reason": f"symlink failed after rename: {error}"})
            continue
        results.append({"system": system, "had_local_collision": True, "renamed_to": old_path.name, "symlink_created": True, "skipped_reason": ""})

    return results


def _revert_system_references(settings: Settings, mount_point: Path, systems: List[dict]) -> List[str]:
    """Precise reversal driven only by our own stored records -- never
    suffix-guessing which local folders it's safe to touch."""
    roms_root = settings.roms_root
    errors: List[str] = []
    for row in systems:
        system = str(row.get("system") or "")
        local_path = roms_root / system
        if row.get("symlink_created") and local_path.is_symlink() and _is_network_reference(local_path, mount_point):
            try:
                local_path.unlink()
            except OSError as error:
                errors.append(f"{system}: could not remove network symlink: {error}")
                continue
        renamed_to = str(row.get("renamed_to") or "")
        if renamed_to and not local_path.exists() and not local_path.is_symlink():
            old_path = roms_root / renamed_to
            if old_path.exists():
                try:
                    old_path.rename(local_path)
                except OSError as error:
                    errors.append(f"{system}: could not restore {renamed_to}: {error}")
        elif renamed_to and (local_path.exists() or local_path.is_symlink()):
            errors.append(f"{system}: original name is occupied; left {renamed_to} in place")
    return errors


# -------------------------------------------------------------- bios refs


def _safe_bios_relative_path(value: object) -> Optional[str]:
    candidate = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(candidate)
    if not candidate or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    if not all(_should_include_entry(part) for part in path.parts):
        return None
    return path.as_posix()


def _list_peer_bios_relative_paths(mount_point: Path, inventory_paths: Optional[List[str]] = None) -> List[str]:
    """Return a validated lexical BIOS inventory.

    A paired peer normally supplies this from its local SQLite cache in the
    inventory summary, avoiding network-filesystem metadata traffic. Walking remains
    a background-only fallback for a peer whose cache/certificate is not ready.
    """
    if inventory_paths is not None:
        return sorted(
            {path for value in inventory_paths if (path := _safe_bios_relative_path(value))},
            key=str.lower,
        )
    peer_bios_dir = mount_point / "bios"
    relative_paths: List[str] = []
    for current_root, directory_names, file_names in os.walk(peer_bios_dir, followlinks=False):
        directory_names[:] = [name for name in directory_names if _should_include_entry(name)]
        root_path = Path(current_root)
        for name in file_names:
            if not _should_include_entry(name):
                continue
            try:
                relative_path = (root_path / name).relative_to(peer_bios_dir).as_posix()
            except ValueError:
                continue
            safe_path = _safe_bios_relative_path(relative_path)
            if safe_path:
                relative_paths.append(safe_path)
    return sorted(set(relative_paths), key=str.lower)


def _apply_bios_references_from_paths(
    settings: Settings,
    mount_point: Path,
    existing_bios: List[dict],
    relative_paths: List[str],
) -> tuple[List[dict], int, int]:
    """Link missing BIOS files from an already-collected lexical inventory."""
    bios_root = settings.bios_root
    peer_bios_dir = mount_point / "bios"
    existing_by_path = {str(row.get("relative_path") or ""): row for row in existing_bios}
    results: List[dict] = []
    local_files_kept = 0
    share_root = network_share_dir(settings)
    for relative_path in relative_paths:
        peer_file = peer_bios_dir / relative_path
        prior = existing_by_path.get(relative_path)
        local_path = bios_root / relative_path

        if prior and prior.get("symlink_created") and _symlink_points_to(local_path, peer_file):
            results.append(prior)
            continue

        if local_path.is_symlink() and _is_network_reference(local_path, share_root):
            if not _is_network_reference(local_path, mount_point):
                local_files_kept += 1
                continue
            try:
                local_path.unlink()
            except OSError:
                local_files_kept += 1
                continue

        if not local_path.exists() and not local_path.is_symlink():
            try:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.symlink_to(peer_file)
            except OSError as error:
                results.append({"relative_path": relative_path, "had_local_collision": False, "renamed_to": "", "symlink_created": False, "skipped_reason": f"symlink failed: {error}"})
                continue
            results.append({"relative_path": relative_path, "had_local_collision": False, "renamed_to": "", "symlink_created": True, "skipped_reason": ""})
            continue

        # Preserve any real local BIOS (and unrelated human-created symlink).
        # The remote copy is merely a fallback for files missing locally.
        local_files_kept += 1

    return results, local_files_kept, len(relative_paths)


def _apply_bios_references(
    settings: Settings,
    mount_point: Path,
    existing_bios: List[dict],
    inventory_paths: Optional[List[str]] = None,
) -> tuple[List[dict], int, int]:
    """Compatibility wrapper used by synchronous tests and recovery tools."""
    try:
        relative_paths = _list_peer_bios_relative_paths(mount_point, inventory_paths)
    except OSError:
        return existing_bios, 0, len(existing_bios)
    return _apply_bios_references_from_paths(settings, mount_point, existing_bios, relative_paths)


def _revert_bios_references(settings: Settings, mount_point: Path, bios_files: List[dict]) -> List[str]:
    """Precise reversal driven only by our own stored records, mirroring
    _revert_system_references. Best-effort removes any now-empty per-emulator
    subfolder this module created along the way (never bios_root itself)."""
    bios_root = settings.bios_root
    errors: List[str] = []
    for row in bios_files:
        relative_path = str(row.get("relative_path") or "")
        if not relative_path:
            continue
        local_path = bios_root / relative_path
        if row.get("symlink_created") and local_path.is_symlink() and _is_network_reference(local_path, mount_point):
            try:
                local_path.unlink()
            except OSError as error:
                errors.append(f"{relative_path}: could not remove network symlink: {error}")
                continue
        renamed_to = str(row.get("renamed_to") or "")
        if renamed_to and not local_path.exists() and not local_path.is_symlink():
            old_path = bios_root / renamed_to
            if old_path.exists():
                try:
                    old_path.rename(local_path)
                except OSError as error:
                    errors.append(f"{relative_path}: could not restore {renamed_to}: {error}")
        elif renamed_to and (local_path.exists() or local_path.is_symlink()):
            errors.append(f"{relative_path}: original name is occupied; left {renamed_to} in place")
        if local_path.parent != bios_root:
            try:
                local_path.parent.rmdir()
            except OSError:
                pass  # not empty, or already gone -- both fine
    return errors


# --------------------------------------------------------------- lifecycle


def _restore_or_remove_owned_link(link: Path, share_root: Path) -> Optional[str]:
    """Remove one Drone-owned link and restore its deterministic ``.old`` peer."""
    if not link.is_symlink() or not _is_network_reference(link, share_root):
        return None
    target = _lexical_symlink_target(link)
    old_path = link.parent / f"{link.name}{NETWORK_SHARE_OLD_SUFFIX}"
    try:
        link.unlink()
    except OSError as error:
        return f"{link}: could not remove network symlink: {error}"
    if old_path.exists() and not link.exists() and not link.is_symlink():
        try:
            old_path.rename(link)
        except OSError as error:
            if target is not None:
                try:
                    link.symlink_to(target, target_is_directory=old_path.is_dir())
                except OSError:
                    pass
            return f"{link}: removed network symlink but could not restore {old_path.name}: {error}"
    return None


def _recover_owned_orphaned_references(settings: Settings, owned_root: Optional[Path] = None) -> List[str]:
    """Recover links left behind after a lost state record or v0.1.129 upgrade.

    ROM references are top-level links.  BIOS references can be nested, so the
    latter uses ``os.walk(..., followlinks=False)`` and inspects only symlinks;
    no target is ever resolved or statted.
    """
    errors: List[str] = []
    share_root = owned_root or network_share_dir(settings)
    try:
        for entry in settings.roms_root.iterdir():
            error = _restore_or_remove_owned_link(entry, share_root)
            if error:
                errors.append(error)
    except OSError as error:
        errors.append(f"could not inspect ROM references: {error}")
    try:
        for current_root, directory_names, file_names in os.walk(settings.bios_root, followlinks=False):
            root_path = Path(current_root)
            # A symlinked directory is a reference too, but must be removed
            # from traversal before os.walk considers descending into it.
            for name in list(directory_names):
                candidate = root_path / name
                if not candidate.is_symlink():
                    continue
                directory_names.remove(name)
                error = _restore_or_remove_owned_link(candidate, share_root)
                if error:
                    errors.append(error)
            for name in file_names:
                error = _restore_or_remove_owned_link(root_path / name, share_root)
                if error:
                    errors.append(error)
    except OSError as error:
        errors.append(f"could not inspect BIOS references: {error}")
    return errors


def migrate_legacy_state(settings: Settings) -> dict:
    """Offline-safe migrations for mount layout and transport metadata.

    v0.1.129 mounted ``.../share/roms`` at the peer root; v0.1.131 mounted the
    whole share and expects a ``roms/`` child.  Reusing the old mount/links
    made every reference dangle.  Restore local content first, unmount the old
    layout, retain enabled peer records, and let boot replay build fresh links.

    Schema v3 only adds the transport identity. Existing v2 mounts are known
    SMB mounts and are deliberately left active; switching a live filesystem
    underneath a running emulator would be less safe than migrating on its
    next controlled remount.
    """
    with _STATE_MIGRATION_LOCK:
        state = _load_state(settings)
        schema_version = int(state.get("schema_version") or 1)
        if schema_version >= NETWORK_SHARE_STATE_SCHEMA_VERSION:
            return {"migrated": False, "errors": []}
        errors: List[str] = []
        if schema_version < NETWORK_SHARE_LAYOUT_SCHEMA_VERSION:
            errors = _recover_owned_orphaned_references(settings)
            for peer_id, share in state["peers"].items():
                mount_point = Path(str(share.get("mount_point") or peer_mount_point(settings, peer_id)))
                _unmount(mount_point)
                share.update(
                    {
                        "systems": [],
                        "bios": [],
                        "bios_local_count": 0,
                        "bios_remote_count": 0,
                        "status": "pending" if share.get("enabled", True) else share.get("status", "pending"),
                        "status_detail": "Network reference layout upgraded; reconnect pending" if share.get("enabled", True) else "",
                        "updated_at": _now_iso(),
                    }
                )
        if not errors:
            for share in state["peers"].values():
                if isinstance(share, dict):
                    share.setdefault("protocol", "smb")
                    share.setdefault("nfs_version", "")
                    share.setdefault("export_path", "")
                    share["updated_at"] = _now_iso()
            state["schema_version"] = NETWORK_SHARE_STATE_SCHEMA_VERSION
        _save_state(settings, state)
        if errors:
            print(f"Network share migration completed with {len(errors)} cleanup error(s): {errors[0]}", file=sys.stderr, flush=True)
        elif schema_version < NETWORK_SHARE_LAYOUT_SCHEMA_VERSION:
            print("Network share layout migration completed", file=sys.stdout, flush=True)
        return {"migrated": True, "errors": errors}


def _fetch_peer_summary(settings: Settings, target: dict) -> Optional[dict]:
    peer = target.get("peer") if isinstance(target.get("peer"), dict) else None
    # A paired live Drone has an mTLS certificate. Avoid an unauthenticated
    # network attempt for legacy/incomplete records (and simple unit fixtures).
    if not peer or not str(peer.get("certificate_path") or "").strip():
        return None
    started = time.monotonic()
    try:
        summary, _address = _peer_get_json_for_peer(
            peer,
            "/v1/api/peer/inventory/summary",
            settings,
            peer_id=str(target.get("peer_id") or ""),
            config={"network_mode": "local_network"},
            timeout=NETWORK_SHARE_PEER_SUMMARY_TIMEOUT_SECONDS,
            overall_deadline=started + NETWORK_SHARE_PEER_SUMMARY_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    return summary if isinstance(summary, dict) else None


def _fetch_peer_bios_paths(settings: Settings, target: dict) -> Optional[List[str]]:
    peer = target.get("peer") if isinstance(target.get("peer"), dict) else None
    if not peer or not str(peer.get("certificate_path") or "").strip():
        return None
    started = time.monotonic()
    try:
        summary, _address = _peer_get_json_for_peer(
            peer,
            "/v1/api/peer/inventory/summary?include_bios_paths=1",
            settings,
            peer_id=str(target.get("peer_id") or ""),
            config={"network_mode": "local_network"},
            timeout=NETWORK_SHARE_PEER_BIOS_INVENTORY_TIMEOUT_SECONDS,
            overall_deadline=started + NETWORK_SHARE_PEER_BIOS_INVENTORY_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    paths = summary.get("bios_paths") if isinstance(summary, dict) else None
    return paths if isinstance(paths, list) else None


def public_record(record: dict) -> dict:
    """Compact API representation; never serialize thousands of BIOS rows."""
    systems = record.get("systems") if isinstance(record.get("systems"), list) else []
    bios = record.get("bios") if isinstance(record.get("bios"), list) else []
    skipped_systems = sum(1 for row in systems if isinstance(row, dict) and row.get("skipped_reason"))
    public = {
        key: value for key, value in record.items()
        if key not in {"systems", "bios", "remote_system_counts"} and not str(key).startswith("_")
    }
    public.update(
        {
            "system_count": len([row for row in systems if isinstance(row, dict) and row.get("symlink_created")]),
            "bios_link_count": len([row for row in bios if isinstance(row, dict) and row.get("symlink_created")]),
            "skipped_count": skipped_systems + int(record.get("bios_local_count") or 0),
        }
    )
    return public


def _restore_local_fallback(settings: Settings, mount_point: Path, record: dict) -> List[str]:
    """Restore every locally-owned path without touching the remote target."""
    errors = _revert_system_references(settings, mount_point, record.get("systems") or [])
    errors.extend(_revert_bios_references(settings, mount_point, record.get("bios") or []))
    # Also recover mutations made immediately before an interrupted manifest
    # commit. Ownership is restricted to this peer's mount root.
    errors.extend(_recover_owned_orphaned_references(settings, mount_point))
    return list(dict.fromkeys(errors))


def _run_bios_reconciliation(
    settings: Settings,
    peer_id: str,
    mount_point: Path,
    target: Optional[dict],
) -> None:
    key = _operation_key(settings, peer_id)
    try:
        with _peer_operation_lock(settings, peer_id):
            record = _get_peer_record(settings, peer_id)
            if not record or not record.get("enabled", True) or record.get("status") == "detaching":
                return
            _upsert_peer_record(
                settings,
                peer_id,
                bios_status="syncing",
                bios_status_detail="Reconciling missing BIOS files in the background",
            )
        try:
            inventory_paths = _fetch_peer_bios_paths(settings, target) if isinstance(target, dict) else None
            relative_paths = _list_peer_bios_relative_paths(mount_point, inventory_paths)
        except OSError as error:
            with _peer_operation_lock(settings, peer_id):
                current = _get_peer_record(settings, peer_id)
                if current and current.get("enabled", True) and current.get("status") != "detaching":
                    _upsert_peer_record(
                        settings,
                        peer_id,
                        bios_status="error",
                        bios_status_detail=f"BIOS inventory unavailable: {error}",
                    )
            return
        with _peer_operation_lock(settings, peer_id):
            record = _get_peer_record(settings, peer_id)
            if not record or not record.get("enabled", True) or record.get("status") != "mounted":
                return
            bios, local_count, remote_count = _apply_bios_references_from_paths(
                settings,
                mount_point,
                record.get("bios") or [],
                relative_paths,
            )
            _upsert_peer_record(
                settings,
                peer_id,
                bios=bios,
                bios_local_count=local_count,
                bios_remote_count=remote_count,
                bios_status="ready",
                bios_status_detail="",
                last_checked_at=_now_iso(),
            )
    finally:
        with _BACKGROUND_JOBS_LOCK:
            _ACTIVE_BIOS_JOBS.discard(key)


def _schedule_bios_reconciliation(
    settings: Settings,
    peer_id: str,
    mount_point: Path,
    target: Optional[dict],
) -> bool:
    key = _operation_key(settings, peer_id)
    with _BACKGROUND_JOBS_LOCK:
        if key in _ACTIVE_BIOS_JOBS:
            return False
        _ACTIVE_BIOS_JOBS.add(key)
    thread = threading.Thread(
        target=_run_bios_reconciliation,
        args=(settings, peer_id, mount_point, target),
        name=f"drone-network-share-bios-{_safe_dirname(peer_id)}",
        daemon=True,
    )
    try:
        thread.start()
    except Exception:
        with _BACKGROUND_JOBS_LOCK:
            _ACTIVE_BIOS_JOBS.discard(key)
        raise
    return True


def enable(settings: Settings, peer_id: str) -> dict:
    peer_id = str(peer_id or "").strip()
    with _peer_operation_lock(settings, peer_id):
        return _enable_locked(settings, peer_id)


def _enable_locked(settings: Settings, peer_id: str) -> dict:
    target = resolve_peer_target(settings, peer_id)
    migration = migrate_legacy_state(settings)
    if migration.get("errors"):
        return _upsert_peer_record(
            settings,
            peer_id,
            peer_name=target["peer_name"],
            tailnet_ip=target["tailnet_ip"],
            mount_point=str(peer_mount_point(settings, peer_id)),
            enabled=True,
            status="error",
            status_detail=f"Could not safely upgrade existing network references: {migration['errors'][0]}",
            last_checked_at=_now_iso(),
        )
    mount_point = peer_mount_point(settings, peer_id)
    prior = _get_peer_record(settings, peer_id)
    if prior and (not prior.get("enabled", True) or prior.get("status") == "detaching"):
        raise ValueError("network reference detach is still in progress")
    if prior and prior.get("status") in {"pending", "enabling"}:
        recovery_errors = _restore_local_fallback(settings, mount_point, prior)
        _unmount(mount_point, detach_immediately=True)
        if recovery_errors:
            return _upsert_peer_record(
                settings,
                peer_id,
                enabled=True,
                status="error",
                status_detail=f"Could not recover an interrupted enable: {recovery_errors[0]}",
                last_checked_at=_now_iso(),
            )
        prior = _upsert_peer_record(settings, peer_id, systems=[], bios=[], bios_status="pending")
    existing_systems = (prior or {}).get("systems") or []
    existing_bios = (prior or {}).get("bios") or []
    mount_was_active = _is_mounted(mount_point)

    _upsert_peer_record(
        settings,
        peer_id,
        peer_name=target["peer_name"],
        tailnet_ip=target["tailnet_ip"],
        mount_point=str(mount_point),
        enabled=True,
        status="enabling",
        status_detail="Mounting the peer ROM library",
        systems=existing_systems,
        bios=existing_bios,
        bios_status="pending",
        protocol=str((prior or {}).get("protocol") or ""),
        last_checked_at=_now_iso(),
    )

    if mount_was_active:
        mount_result = {
            "status": "mounted",
            "address": str((prior or {}).get("mounted_address") or ""),
            "protocol": str((prior or {}).get("protocol") or "smb"),
            "nfs_version": str((prior or {}).get("nfs_version") or ""),
            "export_path": str((prior or {}).get("export_path") or ""),
        }
    else:
        mount_result = _mount_preferred_transport(settings, target, mount_point)
    if mount_result["status"] != "mounted":
        interrupted = {"systems": existing_systems, "bios": existing_bios}
        recovery_errors = _restore_local_fallback(settings, mount_point, interrupted)
        _unmount(mount_point, detach_immediately=True)
        cleanup_safe = not recovery_errors
        record = _upsert_peer_record(
            settings, peer_id, peer_name=target["peer_name"], tailnet_ip=target["tailnet_ip"],
            mount_point=str(mount_point), enabled=True, status="peer_unreachable" if cleanup_safe else "error",
            status_detail=(mount_result.get("detail", "") if cleanup_safe else f"Could not restore local fallback: {recovery_errors[0]}"),
            systems=[] if cleanup_safe else existing_systems,
            bios=[] if cleanup_safe else existing_bios,
            bios_status="pending" if cleanup_safe else "error",
            protocol=str(mount_result.get("protocol") or (prior or {}).get("protocol") or ""),
            transport_fallback_detail=str(mount_result.get("transport_fallback_detail") or ""),
            last_checked_at=_now_iso(),
        )
        result = dict(record)
        result["_refresh_required"] = bool(prior or existing_systems or existing_bios)
        return result

    summary = _fetch_peer_summary(settings, target)
    peer_system_names = summary.get("systems") if isinstance(summary, dict) and isinstance(summary.get("systems"), list) else None
    systems = _apply_system_references(settings, mount_point, existing_systems, peer_system_names)
    raw_remote_system_counts = summary.get("system_counts") if isinstance(summary, dict) and isinstance(summary.get("system_counts"), dict) else {}
    remote_system_counts = {}
    for name, value in raw_remote_system_counts.items():
        try:
            remote_system_counts[str(name)] = max(0, int(value or 0))
        except (TypeError, ValueError):
            continue
    record_updates = dict(
        peer_name=target["peer_name"], tailnet_ip=target["tailnet_ip"],
        mount_point=str(mount_point), mounted_address=str(mount_result.get("address") or (prior or {}).get("mounted_address") or ""),
        protocol=str(mount_result.get("protocol") or (prior or {}).get("protocol") or "smb"),
        nfs_version=str(mount_result.get("nfs_version") or ""),
        export_path=str(mount_result.get("export_path") or ""),
        transport_fallback_detail=str(mount_result.get("transport_fallback_detail") or ""),
        enabled=True, status="mounted", status_detail="", systems=systems, bios=existing_bios,
        bios_status="pending", bios_status_detail="BIOS reconciliation queued",
        remote_system_counts=remote_system_counts,
        remote_rom_count=sum(int(value or 0) for value in remote_system_counts.values()),
        last_checked_at=_now_iso(),
    )
    # Keep synchronous behavior available for focused recovery tools and unit
    # tests; production defaults to the non-blocking worker.
    if not _bios_reconciliation_background_enabled():
        peer_bios_paths = _fetch_peer_bios_paths(settings, target)
        bios, bios_local_count, bios_remote_count = _apply_bios_references(
            settings, mount_point, existing_bios, peer_bios_paths,
        )
        record_updates.update(
            bios=bios,
            bios_local_count=bios_local_count,
            bios_remote_count=bios_remote_count,
            bios_status="ready",
            bios_status_detail="",
        )
    record = _upsert_peer_record(settings, peer_id, **record_updates)
    if _bios_reconciliation_background_enabled():
        _schedule_bios_reconciliation(settings, peer_id, mount_point, target)
    result = dict(record)
    result["_refresh_required"] = not mount_was_active
    return result


def disable(settings: Settings, peer_id: str) -> dict:
    peer_id = str(peer_id or "").strip()
    with _peer_operation_lock(settings, peer_id):
        migration = migrate_legacy_state(settings)
        record = _get_peer_record(settings, peer_id)
        if not record:
            return {"status": "not_found", "peer_id": peer_id}
        # This write is the durable boundary.  Watchdog and boot replay see it
        # before any symlink is touched and can only continue detach afterward.
        record = _upsert_peer_record(
            settings,
            peer_id,
            enabled=False,
            status="detaching",
            status_detail="Restoring local ROM and BIOS paths",
            last_checked_at=_now_iso(),
        )
        if migration.get("errors"):
            return _upsert_peer_record(
                settings,
                peer_id,
                enabled=False,
                status="error",
                status_detail=f"Could not safely upgrade existing network references: {migration['errors'][0]}",
                last_checked_at=_now_iso(),
            )
        mount_point = Path(str(record.get("mount_point") or peer_mount_point(settings, peer_id)))
        errors = _restore_local_fallback(settings, mount_point, record)
        _unmount(mount_point, detach_immediately=True)
        if errors:
            return _upsert_peer_record(
                settings,
                peer_id,
                enabled=False,
                status="error",
                status_detail=f"Could not safely remove every reference: {errors[0]}",
                last_checked_at=_now_iso(),
            )
        if str(record.get("protocol") or "") == "nfs":
            try:
                _revoke_nfs_export(settings, resolve_peer_target(settings, peer_id))
            except Exception:
                # Local recovery is authoritative. The source restricts the
                # lingering read-only export to this still-paired Drone and
                # revokes it when the peer is forgotten or reauthorized.
                pass
        _delete_peer_record(settings, peer_id)
        return {"status": "disabled", "peer_id": peer_id}


def request_disable(settings: Settings, peer_id: str) -> dict:
    """Persist detach intent and finish cleanup on a daemon thread."""
    peer_id = str(peer_id or "").strip()
    key = _operation_key(settings, peer_id)
    with _peer_operation_lock(settings, peer_id):
        record = _get_peer_record(settings, peer_id)
        if not record:
            return {"status": "not_found", "peer_id": peer_id}
        record = _upsert_peer_record(
            settings,
            peer_id,
            enabled=False,
            status="detaching",
            status_detail="Detach accepted; restoring local ROM and BIOS paths",
            last_checked_at=_now_iso(),
        )
        with _BACKGROUND_JOBS_LOCK:
            if key in _ACTIVE_DISABLE_JOBS:
                return public_record(record)
            _ACTIVE_DISABLE_JOBS.add(key)

    def finish() -> None:
        try:
            result = disable(settings, peer_id)
            if result.get("status") == "disabled":
                _refresh_emulationstation_after_share_change()
        except Exception as error:
            with _peer_operation_lock(settings, peer_id):
                if _get_peer_record(settings, peer_id):
                    _upsert_peer_record(
                        settings,
                        peer_id,
                        enabled=False,
                        status="error",
                        status_detail=f"Detach failed and will be retried after restart: {error}",
                        last_checked_at=_now_iso(),
                    )
        finally:
            with _BACKGROUND_JOBS_LOCK:
                _ACTIVE_DISABLE_JOBS.discard(key)

    try:
        threading.Thread(
            target=finish,
            name=f"drone-network-share-detach-{_safe_dirname(peer_id)}",
            daemon=True,
        ).start()
    except Exception:
        with _BACKGROUND_JOBS_LOCK:
            _ACTIVE_DISABLE_JOBS.discard(key)
        raise
    return public_record(record)


def status(settings: Settings) -> List[dict]:
    """Cheap read: stored rows plus a non-blocking `os.path.ismount()` sanity
    check per peer. Deliberately does not do a network-touching liveness probe
    synchronously -- an admin-page load must never block on a `soft` mount's
    worst-case timeout; that's the watchdog's job."""
    shares = list_shares(settings)
    for share in shares:
        mount_point = Path(str(share.get("mount_point") or ""))
        if share.get("status") == "mounted" and mount_point and not _is_mounted(mount_point):
            share["status"] = "peer_unreachable"
    return [public_record(share) for share in shares]


def _probe_mount_alive(mount_point: Path) -> bool:
    if not _is_mounted(mount_point):
        return False
    try:
        next(mount_point.iterdir(), None)
        return True
    except OSError:
        return False


def _refresh_emulationstation_after_share_change() -> bool:
    if str(os.environ.get("DRONE_NETWORK_SHARE_AUTO_REFRESH_ES", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return False
    try:
        from .device_control import _restart_emulationstation
    except ImportError:  # pragma: no cover - direct script execution fallback
        from device.device_control import _restart_emulationstation  # type: ignore
    try:
        return bool(_restart_emulationstation())
    except Exception:
        return False


def maybe_reconnect_all_on_boot(settings: Settings) -> None:
    """Best-effort replay of every configured reference on Drone startup --
    never raises, logs and continues per-row, same defensive style as
    vpn_manager.maybe_auto_connect."""
    migrate_legacy_state(settings)
    refresh_required = False
    for share in list_shares(settings):
        peer_id = str(share.get("peer_id") or "")
        if not peer_id:
            continue
        try:
            if not share.get("enabled", True) or share.get("status") == "detaching":
                result = disable(settings, peer_id)
                refresh_required = refresh_required or result.get("status") == "disabled"
            else:
                result = enable(settings, peer_id)
                refresh_required = refresh_required or bool(result.get("_refresh_required"))
        except Exception as error:
            print(f"Network share boot replay failed for {share.get('peer_name') or peer_id}: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)
    if refresh_required and not _refresh_emulationstation_after_share_change():
        print("Network share boot replay completed, but EmulationStation could not be refreshed", file=sys.stderr, flush=True)


def run_watchdog_poller(settings: Settings) -> None:
    """Forever-loop: verify every enabled share's mount is still alive and
    remount if not. Started as its own daemon thread from create_server(),
    same pattern as VPN's run_self_heal_poller."""
    while True:
        time.sleep(NETWORK_SHARE_WATCHDOG_INTERVAL_SECONDS)
        for share in list_shares(settings):
            peer_id = str(share.get("peer_id") or "")
            if not peer_id or not share.get("enabled", True) or share.get("status") == "detaching":
                continue
            mount_point = Path(str(share.get("mount_point") or ""))
            alive = bool(mount_point and _probe_mount_alive(mount_point))
            refresh_required = False
            try:
                # Re-check under the same lock detach uses.  A watchdog probe
                # may have started from a stale enabled snapshot just before
                # request_disable persisted its intent.
                with _peer_operation_lock(settings, peer_id):
                    current = _get_peer_record(settings, peer_id)
                    if not current or not current.get("enabled", True) or current.get("status") == "detaching":
                        continue
                    if alive:
                        if current.get("status") != "mounted":
                            _upsert_peer_record(settings, peer_id, status="mounted", status_detail="", last_checked_at=_now_iso())
                        continue
                    _unmount(mount_point)
                    result = enable(settings, peer_id)
                    refresh_required = bool(result.get("_refresh_required"))
            except Exception as error:
                with _peer_operation_lock(settings, peer_id):
                    current = _get_peer_record(settings, peer_id)
                    if current and current.get("enabled", True):
                        _upsert_peer_record(settings, peer_id, status="peer_unreachable", status_detail=str(error), last_checked_at=_now_iso())
            if refresh_required:
                _refresh_emulationstation_after_share_change()
