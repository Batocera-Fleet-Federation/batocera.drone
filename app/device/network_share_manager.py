"""Reference a paired peer's whole ROM library and BIOS folder over SMB/CIFS
instead of copying them: mount the peer's Batocera Samba share
(``//<peer tailnet ip>/share``, Batocera's own stock export -- nothing on the
peer side is set up by Drone) once, then reconcile both ``roms/`` and
``bios/`` under it into this Drone's own ``roms_root``/``bios_root``.

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
peer-health summaries never walk a latency-sensitive SMB tree.

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
guest/anonymous only (matches Batocera's default open Samba config on a
private tailnet) and read-only + ``soft`` (bounded failure instead of hanging
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
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
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
    from ..transfer.peer_connectivity import _peer_get_json_for_peer
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
    from transfer.peer_connectivity import _peer_get_json_for_peer  # type: ignore

NETWORK_SHARE_STATE_NAMESPACE = "network_share_manager.json"
NETWORK_SHARE_STATUSES = ("mounted", "peer_unreachable", "error", "pending")
NETWORK_SHARE_OLD_SUFFIX = ".old"
NETWORK_SHARE_STATE_SCHEMA_VERSION = 2

NETWORK_SHARE_MOUNT_TIMEOUT_SECONDS = float(os.environ.get("DRONE_NETWORK_SHARE_MOUNT_TIMEOUT_SECONDS", "20"))
NETWORK_SHARE_UMOUNT_TIMEOUT_SECONDS = float(os.environ.get("DRONE_NETWORK_SHARE_UMOUNT_TIMEOUT_SECONDS", "10"))
# How often the watchdog re-probes every enabled share. Deliberately simpler
# than VPN's rate-limited self-heal (no attempts-per-window backoff): a failed
# CIFS mount attempt is cheap and already bounded by the `soft` mount option,
# unlike VPN's remote-provider reconnect which can be a slow/costly handshake.
NETWORK_SHARE_WATCHDOG_INTERVAL_SECONDS = float(os.environ.get("DRONE_NETWORK_SHARE_WATCHDOG_INTERVAL_SECONDS", "60"))
NETWORK_SHARE_ATTRIBUTE_CACHE_SECONDS = max(1, int(os.environ.get("DRONE_NETWORK_SHARE_ACTIMEO_SECONDS", "30")))
NETWORK_SHARE_PEER_SUMMARY_TIMEOUT_SECONDS = float(os.environ.get("DRONE_NETWORK_SHARE_PEER_SUMMARY_TIMEOUT_SECONDS", "6"))

_STATE_MIGRATION_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
    stored = _load_state_payload(_state_database_path(settings.userdata_root), NETWORK_SHARE_STATE_NAMESPACE, {})
    stored = stored if isinstance(stored, dict) else {}
    peers = stored.get("peers")
    try:
        schema_version = int(stored.get("schema_version") or 1)
    except (TypeError, ValueError):
        schema_version = 1
    return {"schema_version": schema_version, "peers": peers if isinstance(peers, dict) else {}}


def _save_state(settings: Settings, state: dict) -> None:
    payload = {
        "schema_version": int(state.get("schema_version") or NETWORK_SHARE_STATE_SCHEMA_VERSION),
        "peers": state.get("peers") if isinstance(state.get("peers"), dict) else {},
    }
    _save_state_payload(_state_database_path(settings.userdata_root), NETWORK_SHARE_STATE_NAMESPACE, payload)


def _get_peer_record(settings: Settings, peer_id: str) -> Optional[dict]:
    return _load_state(settings)["peers"].get(str(peer_id or "").strip())


def _upsert_peer_record(settings: Settings, peer_id: str, **updates) -> dict:
    state = _load_state(settings)
    peer_id = str(peer_id or "").strip()
    record = state["peers"].get(peer_id) or {
        "peer_id": peer_id, "peer_name": "", "tailnet_ip": "", "mount_point": "",
        "enabled": True, "status": "pending", "status_detail": "",
        "systems": [], "bios": [], "created_at": _now_iso(), "last_checked_at": None,
    }
    record.update(updates)
    record["updated_at"] = _now_iso()
    state["peers"][peer_id] = record
    _save_state(settings, state)
    return record


def _delete_peer_record(settings: Settings, peer_id: str) -> None:
    state = _load_state(settings)
    state["peers"].pop(str(peer_id or "").strip(), None)
    _save_state(settings, state)


def list_shares(settings: Settings) -> List[dict]:
    return sorted(_load_state(settings)["peers"].values(), key=lambda row: str(row.get("peer_name") or row.get("peer_id") or "").lower())


def get_share(settings: Settings, peer_id: str) -> Optional[dict]:
    return _get_peer_record(settings, peer_id)


# ---------------------------------------------------------------- resolve


def resolve_peer_target(settings: Settings, peer_id: str) -> dict:
    """Resolve trusted LAN-first SMB candidates for a paired peer.

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
    try:
        return os.path.ismount(str(mount_point))
    except OSError:
        return False


def _smb_port_open(address: str, timeout: float = 1.0) -> bool:
    """Cheaply reject an unreachable candidate before invoking mount.cifs."""
    try:
        with socket.create_connection((address, 445), timeout=max(0.1, timeout)):
            return True
    except OSError:
        return False


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
                    f"guest,ro,soft,actimeo={NETWORK_SHARE_ATTRIBUTE_CACHE_SECONDS}",
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


def _unmount(mount_point: Path) -> None:
    if not _is_mounted(mount_point):
        return
    try:
        result = subprocess.run(["umount", str(mount_point)], capture_output=True, text=True, timeout=NETWORK_SHARE_UMOUNT_TIMEOUT_SECONDS)
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
            # scandir receives the directory-entry type from Samba's readdir
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

        if prior and prior.get("symlink_created") and _symlink_points_to(local_path, target):
            results.append(prior)
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
            results.append({"system": system, "had_local_collision": False, "renamed_to": "", "symlink_created": True, "skipped_reason": ""})
            continue

        old_path = roms_root / f"{system}{NETWORK_SHARE_OLD_SUFFIX}"
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


def _apply_bios_references(settings: Settings, mount_point: Path, existing_bios: List[dict]) -> tuple[List[dict], int, int]:
    """Link only BIOS files the satellite does not already have.

    Local BIOS wins.  Replacing thousands of existing local files with remote
    links made every BIOS scan depend on SMB and provided no benefit on a
    normally provisioned Batocera installation.  ``os.walk`` consumes Samba's
    directory listings without an ``is_file``/``stat`` request per file.

    Returns ``(created_or_retained_links, local_files_kept, remote_files_seen)``.
    """
    bios_root = settings.bios_root
    peer_bios_dir = mount_point / "bios"
    existing_by_path = {str(row.get("relative_path") or ""): row for row in existing_bios}
    peer_files: List[Path] = []
    try:
        for current_root, directory_names, file_names in os.walk(peer_bios_dir, followlinks=False):
            directory_names[:] = [name for name in directory_names if _should_include_entry(name)]
            root_path = Path(current_root)
            peer_files.extend(root_path / name for name in file_names if _should_include_entry(name))
        peer_files.sort(key=lambda path: path.relative_to(peer_bios_dir).as_posix().lower())
    except OSError:
        return existing_bios, 0, len(existing_bios)  # mount unreadable right now -- watchdog will retry

    results: List[dict] = []
    local_files_kept = 0
    share_root = network_share_dir(settings)
    for peer_file in peer_files:
        try:
            relative_path = peer_file.relative_to(peer_bios_dir).as_posix()
        except ValueError:
            continue
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

    return results, local_files_kept, len(peer_files)


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


def _recover_owned_orphaned_references(settings: Settings) -> List[str]:
    """Recover links left behind after a lost state record or v0.1.129 upgrade.

    ROM references are top-level links.  BIOS references can be nested, so the
    latter uses ``os.walk(..., followlinks=False)`` and inspects only symlinks;
    no target is ever resolved or statted.
    """
    errors: List[str] = []
    share_root = network_share_dir(settings)
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
    """One-time offline-safe migration from the original mount layout.

    v0.1.129 mounted ``.../share/roms`` at the peer root; v0.1.131 mounted the
    whole share and expects a ``roms/`` child.  Reusing the old mount/links
    made every reference dangle.  Restore local content first, unmount the old
    layout, retain enabled peer records, and let boot replay build fresh links.
    """
    with _STATE_MIGRATION_LOCK:
        state = _load_state(settings)
        if int(state.get("schema_version") or 1) >= NETWORK_SHARE_STATE_SCHEMA_VERSION:
            return {"migrated": False, "errors": []}
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
            state["schema_version"] = NETWORK_SHARE_STATE_SCHEMA_VERSION
        _save_state(settings, state)
        if errors:
            print(f"Network share migration completed with {len(errors)} cleanup error(s): {errors[0]}", file=sys.stderr, flush=True)
        else:
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


def public_record(record: dict) -> dict:
    """Compact API representation; never serialize thousands of BIOS rows."""
    systems = record.get("systems") if isinstance(record.get("systems"), list) else []
    bios = record.get("bios") if isinstance(record.get("bios"), list) else []
    skipped_systems = sum(1 for row in systems if isinstance(row, dict) and row.get("skipped_reason"))
    public = {key: value for key, value in record.items() if key not in {"systems", "bios", "remote_system_counts"}}
    public.update(
        {
            "system_count": len([row for row in systems if isinstance(row, dict) and row.get("symlink_created")]),
            "bios_link_count": len([row for row in bios if isinstance(row, dict) and row.get("symlink_created")]),
            "skipped_count": skipped_systems + int(record.get("bios_local_count") or 0),
        }
    )
    return public


def enable(settings: Settings, peer_id: str) -> dict:
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
    existing_systems = (prior or {}).get("systems") or []
    existing_bios = (prior or {}).get("bios") or []

    mount_result = _mount(target["addresses"], mount_point)
    if mount_result["status"] != "mounted":
        record = _upsert_peer_record(
            settings, peer_id, peer_name=target["peer_name"], tailnet_ip=target["tailnet_ip"],
            mount_point=str(mount_point), enabled=True, status="error",
            status_detail=mount_result.get("detail", ""), systems=existing_systems, bios=existing_bios, last_checked_at=_now_iso(),
        )
        return record

    summary = _fetch_peer_summary(settings, target)
    peer_system_names = summary.get("systems") if isinstance(summary, dict) and isinstance(summary.get("systems"), list) else None
    systems = _apply_system_references(settings, mount_point, existing_systems, peer_system_names)
    bios, bios_local_count, bios_remote_count = _apply_bios_references(settings, mount_point, existing_bios)
    raw_remote_system_counts = summary.get("system_counts") if isinstance(summary, dict) and isinstance(summary.get("system_counts"), dict) else {}
    remote_system_counts = {}
    for name, value in raw_remote_system_counts.items():
        try:
            remote_system_counts[str(name)] = max(0, int(value or 0))
        except (TypeError, ValueError):
            continue
    record = _upsert_peer_record(
        settings, peer_id, peer_name=target["peer_name"], tailnet_ip=target["tailnet_ip"],
        mount_point=str(mount_point), mounted_address=str(mount_result.get("address") or (prior or {}).get("mounted_address") or ""),
        enabled=True, status="mounted", status_detail="", systems=systems, bios=bios,
        bios_local_count=bios_local_count, bios_remote_count=bios_remote_count,
        remote_system_counts=remote_system_counts,
        remote_rom_count=sum(int(value or 0) for value in remote_system_counts.values()),
        last_checked_at=_now_iso(),
    )
    return record


def disable(settings: Settings, peer_id: str) -> dict:
    migration = migrate_legacy_state(settings)
    record = _get_peer_record(settings, peer_id)
    if not record:
        return {"status": "not_found", "peer_id": peer_id}
    if migration.get("errors"):
        return _upsert_peer_record(
            settings,
            peer_id,
            enabled=True,
            status="error",
            status_detail=f"Could not safely upgrade existing network references: {migration['errors'][0]}",
            last_checked_at=_now_iso(),
        )
    mount_point = Path(str(record.get("mount_point") or peer_mount_point(settings, peer_id)))
    errors = _revert_system_references(settings, mount_point, record.get("systems") or [])
    errors.extend(_revert_bios_references(settings, mount_point, record.get("bios") or []))
    _unmount(mount_point)
    if errors:
        return _upsert_peer_record(
            settings,
            peer_id,
            enabled=True,
            status="error",
            status_detail=f"Could not safely remove every reference: {errors[0]}",
            last_checked_at=_now_iso(),
        )
    _delete_peer_record(settings, peer_id)
    return {"status": "disabled", "peer_id": peer_id}


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


def maybe_reconnect_all_on_boot(settings: Settings) -> None:
    """Best-effort replay of every configured reference on Drone startup --
    never raises, logs and continues per-row, same defensive style as
    vpn_manager.maybe_auto_connect."""
    migrate_legacy_state(settings)
    for share in list_shares(settings):
        peer_id = str(share.get("peer_id") or "")
        if not peer_id or not share.get("enabled", True):
            continue
        try:
            enable(settings, peer_id)
        except Exception as error:
            print(f"Network share boot replay failed for {share.get('peer_name') or peer_id}: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)


def run_watchdog_poller(settings: Settings) -> None:
    """Forever-loop: verify every enabled share's mount is still alive and
    remount if not. Started as its own daemon thread from create_server(),
    same pattern as VPN's run_self_heal_poller."""
    while True:
        time.sleep(NETWORK_SHARE_WATCHDOG_INTERVAL_SECONDS)
        for share in list_shares(settings):
            peer_id = str(share.get("peer_id") or "")
            if not peer_id or not share.get("enabled", True):
                continue
            mount_point = Path(str(share.get("mount_point") or ""))
            if mount_point and _probe_mount_alive(mount_point):
                if share.get("status") != "mounted":
                    _upsert_peer_record(settings, peer_id, status="mounted", status_detail="", last_checked_at=_now_iso())
                continue
            try:
                _unmount(mount_point)
                enable(settings, peer_id)
            except Exception as error:
                _upsert_peer_record(settings, peer_id, status="peer_unreachable", status_detail=str(error), last_checked_at=_now_iso())
