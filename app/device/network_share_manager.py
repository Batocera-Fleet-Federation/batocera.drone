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
a browsable catalog the way ROM systems are): same rename-aside-then-symlink
behavior, just applied per file (``<name>.old``) rather than per directory.

``.old`` is not a new convention: ``RomRepository.should_include_system``
already excludes any ``.old``-suffixed folder from Drone's own system list
(reused here for ROMs), and a bare ``.old`` suffix also isn't a recognized
BIOS extension, so ``RomAssetBiosMixin.list_bios_entries``'s extension filter
already excludes it too, for free. Both are also the standard upstream
Batocera/EmulationStation trick for "keep on disk, don't show" -- reusing them
means zero new hide-logic and zero changes to any scanning/gamelist code,
since ``get_system_dir``/``list_system_names`` (``roms/rom_systems.py``)
already transparently follow a symlink via ``.resolve()`` and the ROM
scanner's ``rglob`` follows it too, and a BIOS *file* symlink (as opposed to a
symlinked directory) is picked up by ``os.walk`` regardless of its
``followlinks`` setting -- which is exactly why BIOS is mirrored file-by-file
with real (non-symlink) parent directories, not by symlinking a whole
per-emulator subfolder the way ROM systems are.

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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

try:
    from ..common.install_paths import drone_install_root as _drone_install_root
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..transfer import local_network as _local_network
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.install_paths import drone_install_root as _drone_install_root  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from transfer import local_network as _local_network  # type: ignore

NETWORK_SHARE_STATE_NAMESPACE = "network_share_manager.json"
NETWORK_SHARE_STATUSES = ("mounted", "peer_unreachable", "error", "pending")
NETWORK_SHARE_OLD_SUFFIX = ".old"

NETWORK_SHARE_MOUNT_TIMEOUT_SECONDS = float(os.environ.get("DRONE_NETWORK_SHARE_MOUNT_TIMEOUT_SECONDS", "20"))
NETWORK_SHARE_UMOUNT_TIMEOUT_SECONDS = float(os.environ.get("DRONE_NETWORK_SHARE_UMOUNT_TIMEOUT_SECONDS", "10"))
# How often the watchdog re-probes every enabled share. Deliberately simpler
# than VPN's rate-limited self-heal (no attempts-per-window backoff): a failed
# CIFS mount attempt is cheap and already bounded by the `soft` mount option,
# unlike VPN's remote-provider reconnect which can be a slow/costly handshake.
NETWORK_SHARE_WATCHDOG_INTERVAL_SECONDS = float(os.environ.get("DRONE_NETWORK_SHARE_WATCHDOG_INTERVAL_SECONDS", "60"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _should_include_entry(name: str) -> bool:
    # Mirrors RomRepository.should_include_system (app/drone_api.py) without
    # importing it -- used for both ROM system directory names and BIOS file
    # names, so a peer's own hidden/renamed-aside entries never get mirrored
    # back in as if they were real content.
    return not str(name or "").strip().lower().endswith(NETWORK_SHARE_OLD_SUFFIX)


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
    return {"peers": peers if isinstance(peers, dict) else {}}


def _save_state(settings: Settings, state: dict) -> None:
    _save_state_payload(_state_database_path(settings.userdata_root), NETWORK_SHARE_STATE_NAMESPACE, state)


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
    """Look up a paired peer's own tailnet IP -- server-side only, never from
    client-supplied input, so a mount can only ever target an actual paired
    swarm peer."""
    peer_id = str(peer_id or "").strip()
    if not peer_id:
        raise ValueError("peer_id is required")
    peer = _local_network.get_paired_peer(settings, peer_id)
    if not peer:
        raise ValueError("not a paired peer")
    tailnet_ip = str(peer.get("tailnet_ip") or "").strip()
    if not tailnet_ip:
        raise ValueError("peer has no known Tailscale address")
    return {"peer_id": peer_id, "peer_name": str(peer.get("name") or peer.get("hostname") or peer_id), "tailnet_ip": tailnet_ip}


# ------------------------------------------------------------------ mount


def _is_mounted(mount_point: Path) -> bool:
    try:
        return os.path.ismount(str(mount_point))
    except OSError:
        return False


def _mount(tailnet_ip: str, mount_point: Path) -> dict:
    mount_point.mkdir(parents=True, exist_ok=True)
    if _is_mounted(mount_point):
        return {"status": "mounted"}
    try:
        result = subprocess.run(
            # The whole share, not just .../share/roms -- roms/ and bios/ are
            # both subfolders of it, so one mount covers both.
            ["mount", "-t", "cifs", f"//{tailnet_ip}/share", str(mount_point), "-o", "guest,ro,soft"],
            capture_output=True, text=True, timeout=NETWORK_SHARE_MOUNT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"status": "error", "detail": f"Failed to run mount: {error}"}
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return {"status": "error", "detail": detail[-1] if detail else f"mount exited with status {result.returncode}"}
    return {"status": "mounted"}


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


def _apply_system_references(settings: Settings, mount_point: Path, existing_systems: List[dict]) -> List[dict]:
    """Reconcile local roms_root against everything the mount's roms/ export
    currently exposes. Idempotent -- safe to call again on every boot replay /
    watchdog pass without re-doing work that's already correctly in place."""
    roms_root = settings.roms_root
    peer_roms_dir = mount_point / "roms"
    existing_by_name = {str(row.get("system") or ""): row for row in existing_systems}
    try:
        peer_systems = sorted(entry.name for entry in peer_roms_dir.iterdir() if entry.is_dir() and _should_include_entry(entry.name))
    except OSError:
        return existing_systems  # mount unreadable right now -- leave prior state untouched, watchdog will retry

    results: List[dict] = []
    for system in peer_systems:
        prior = existing_by_name.get(system)
        local_path = roms_root / system
        target = peer_roms_dir / system

        if prior and prior.get("symlink_created") and local_path.is_symlink():
            try:
                already_correct = local_path.resolve() == target.resolve()
            except OSError:
                already_correct = False
            if already_correct:
                results.append(prior)
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
            local_path.symlink_to(target, target_is_directory=True)
        except OSError as error:
            results.append({"system": system, "had_local_collision": True, "renamed_to": "", "symlink_created": False, "skipped_reason": f"rename/symlink failed: {error}"})
            continue
        results.append({"system": system, "had_local_collision": True, "renamed_to": old_path.name, "symlink_created": True, "skipped_reason": ""})

    return results


def _revert_system_references(settings: Settings, mount_point: Path, systems: List[dict]) -> None:
    """Precise reversal driven only by our own stored records -- never
    suffix-guessing which local folders it's safe to touch."""
    roms_root = settings.roms_root
    for row in systems:
        if not row.get("symlink_created"):
            continue
        system = str(row.get("system") or "")
        local_path = roms_root / system
        if local_path.is_symlink():
            try:
                # Safety check: only remove it if it still points into our
                # own mount -- if a human repointed/replaced it since, leave
                # it alone rather than deleting something we no longer own.
                if local_path.resolve().is_relative_to(mount_point.resolve()):
                    local_path.unlink()
            except OSError:
                continue
        renamed_to = str(row.get("renamed_to") or "")
        if renamed_to and not local_path.exists() and not local_path.is_symlink():
            old_path = roms_root / renamed_to
            if old_path.exists():
                try:
                    old_path.rename(local_path)
                except OSError:
                    pass


# -------------------------------------------------------------- bios refs


def _apply_bios_references(settings: Settings, mount_point: Path, existing_bios: List[dict]) -> List[dict]:
    """Reconcile local bios_root against every file the mount's bios/ export
    currently has, file by file rather than directory by directory -- unlike
    ROM systems, BIOS is mostly individual files (some nested one level under
    a per-emulator subfolder like ``dc/``), not a directory worth referencing
    wholesale. Idempotent, same shape as _apply_system_references."""
    bios_root = settings.bios_root
    peer_bios_dir = mount_point / "bios"
    existing_by_path = {str(row.get("relative_path") or ""): row for row in existing_bios}
    try:
        peer_files = sorted(
            entry for entry in peer_bios_dir.rglob("*")
            if entry.is_file() and _should_include_entry(entry.name)
        )
    except OSError:
        return existing_bios  # mount unreadable right now -- leave prior state untouched, watchdog will retry

    results: List[dict] = []
    for peer_file in peer_files:
        try:
            relative_path = peer_file.relative_to(peer_bios_dir).as_posix()
        except ValueError:
            continue
        prior = existing_by_path.get(relative_path)
        local_path = bios_root / relative_path

        if prior and prior.get("symlink_created") and local_path.is_symlink():
            try:
                already_correct = local_path.resolve() == peer_file.resolve()
            except OSError:
                already_correct = False
            if already_correct:
                results.append(prior)
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

        old_path = local_path.parent / f"{local_path.name}{NETWORK_SHARE_OLD_SUFFIX}"
        if old_path.exists() or old_path.is_symlink():
            results.append({"relative_path": relative_path, "had_local_collision": True, "renamed_to": "", "symlink_created": False, "skipped_reason": f"skipped: {old_path.name} already exists"})
            continue
        try:
            local_path.rename(old_path)
            local_path.symlink_to(peer_file)
        except OSError as error:
            results.append({"relative_path": relative_path, "had_local_collision": True, "renamed_to": "", "symlink_created": False, "skipped_reason": f"rename/symlink failed: {error}"})
            continue
        renamed_to = old_path.relative_to(bios_root).as_posix()
        results.append({"relative_path": relative_path, "had_local_collision": True, "renamed_to": renamed_to, "symlink_created": True, "skipped_reason": ""})

    return results


def _revert_bios_references(settings: Settings, mount_point: Path, bios_files: List[dict]) -> None:
    """Precise reversal driven only by our own stored records, mirroring
    _revert_system_references. Best-effort removes any now-empty per-emulator
    subfolder this module created along the way (never bios_root itself)."""
    bios_root = settings.bios_root
    peer_bios_dir = mount_point / "bios"
    for row in bios_files:
        if not row.get("symlink_created"):
            continue
        relative_path = str(row.get("relative_path") or "")
        if not relative_path:
            continue
        local_path = bios_root / relative_path
        if local_path.is_symlink():
            try:
                # Safety check: only remove it if it still points into our
                # own mount -- if a human repointed/replaced it since, leave
                # it alone rather than deleting something we no longer own.
                if local_path.resolve().is_relative_to(peer_bios_dir.resolve()):
                    local_path.unlink()
            except OSError:
                continue
        renamed_to = str(row.get("renamed_to") or "")
        if renamed_to and not local_path.exists() and not local_path.is_symlink():
            old_path = bios_root / renamed_to
            if old_path.exists():
                try:
                    old_path.rename(local_path)
                except OSError:
                    pass
        if local_path.parent != bios_root:
            try:
                local_path.parent.rmdir()
            except OSError:
                pass  # not empty, or already gone -- both fine


# --------------------------------------------------------------- lifecycle


def enable(settings: Settings, peer_id: str) -> dict:
    target = resolve_peer_target(settings, peer_id)
    mount_point = peer_mount_point(settings, peer_id)
    prior = _get_peer_record(settings, peer_id)
    existing_systems = (prior or {}).get("systems") or []
    existing_bios = (prior or {}).get("bios") or []

    mount_result = _mount(target["tailnet_ip"], mount_point)
    if mount_result["status"] != "mounted":
        record = _upsert_peer_record(
            settings, peer_id, peer_name=target["peer_name"], tailnet_ip=target["tailnet_ip"],
            mount_point=str(mount_point), enabled=True, status="error",
            status_detail=mount_result.get("detail", ""), systems=existing_systems, bios=existing_bios, last_checked_at=_now_iso(),
        )
        return record

    systems = _apply_system_references(settings, mount_point, existing_systems)
    bios = _apply_bios_references(settings, mount_point, existing_bios)
    record = _upsert_peer_record(
        settings, peer_id, peer_name=target["peer_name"], tailnet_ip=target["tailnet_ip"],
        mount_point=str(mount_point), enabled=True, status="mounted",
        status_detail="", systems=systems, bios=bios, last_checked_at=_now_iso(),
    )
    return record


def disable(settings: Settings, peer_id: str) -> dict:
    record = _get_peer_record(settings, peer_id)
    if not record:
        return {"status": "not_found", "peer_id": peer_id}
    mount_point = Path(str(record.get("mount_point") or peer_mount_point(settings, peer_id)))
    _revert_system_references(settings, mount_point, record.get("systems") or [])
    _revert_bios_references(settings, mount_point, record.get("bios") or [])
    _unmount(mount_point)
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
    return shares


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
                enable(settings, peer_id)
            except Exception as error:
                _upsert_peer_record(settings, peer_id, status="peer_unreachable", status_detail=str(error), last_checked_at=_now_iso())
