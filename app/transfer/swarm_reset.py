"""Targeted removal of identity-bound swarm state for a freshly provisioned Drone.

A cloned disk carries the source Drone's paired peers, pinned peer certificates,
membership roster and tombstones. This module clears exactly that -- in one SQLite
transaction plus the pinned-certificate directory -- and leaves everything else
(credentials, ROM/BIOS/artwork caches, emulator and UI settings) alone.

It never goes through ``forget_peer`` / ``publish_forget`` / ``record_local_removal``:
a clone is a *new* device, so nothing here may announce a removal or rotation on
behalf of the original Drone. Every step is idempotent so an interrupted
provisioning run can simply repeat it.
"""

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

try:
    from ..storage.state_store import database_path, open_database
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path, open_database  # type: ignore

# app_state namespaces that exist only because of the inherited swarm identity.
IDENTITY_BOUND_NAMESPACES = (
    "local_paired_peers",
    "local_discovered_peers",
    "local_pairing_code",
    "local_peer_checks",
    "tailnet_forgotten_peers",
    "swarm_membership",
    "network_reference_selection.json",
    "network_share_manager.json",
    "nfs_export_manager.json",
)
IDENTITY_BOUND_EVENT_NAMESPACES = ("local_sync_activity",)
DOWNLOAD_QUEUE_NAMESPACE = "download_manager.json"
CLONE_REVOCATION_REASON = (
    "This Drone was cloned onto new hardware and is no longer part of the swarm that shared this."
)


def _log(message: str) -> None:
    print(f"[Identity] {message}", file=sys.stdout, flush=True)


def _reset_database(settings: Any) -> Dict[str, int]:
    removed: Dict[str, int] = {}
    with open_database(database_path(Path(settings.userdata_root))) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for namespace in IDENTITY_BOUND_NAMESPACES:
            cursor = connection.execute("DELETE FROM app_state WHERE namespace = ?", (namespace,))
            removed[namespace] = max(0, cursor.rowcount or 0)
        for namespace in IDENTITY_BOUND_EVENT_NAMESPACES:
            cursor = connection.execute("DELETE FROM app_events WHERE namespace = ?", (namespace,))
            removed[namespace] = max(0, cursor.rowcount or 0)
        cursor = connection.execute("DELETE FROM peer_route_cache")
        removed["peer_route_cache"] = max(0, cursor.rowcount or 0)
        # Queued transfers that name a source peer are meaningless without that peer;
        # unrelated queue state (the paused flag) is kept.
        row = connection.execute(
            "SELECT payload FROM app_state WHERE namespace = ? AND state_key = 'payload'",
            (DOWNLOAD_QUEUE_NAMESPACE,),
        ).fetchone()
        if row:
            try:
                queue = json.loads(row[0])
            except ValueError:
                queue = None
            if isinstance(queue, dict) and isinstance(queue.get("jobs"), list):
                kept = [
                    job for job in queue["jobs"]
                    if not (isinstance(job, dict) and str(job.get("source_drone_id") or "").strip())
                ]
                removed["download_jobs"] = len(queue["jobs"]) - len(kept)
                if removed["download_jobs"]:
                    queue["jobs"] = kept
                    connection.execute(
                        "UPDATE app_state SET payload = ? WHERE namespace = ? AND state_key = 'payload'",
                        (json.dumps(queue, sort_keys=True, default=str), DOWNLOAD_QUEUE_NAMESPACE),
                    )
        connection.commit()
    return removed


def _clear_pinned_peer_certificates(settings: Any) -> int:
    directory = Path(settings.userdata_root) / "system" / "drone-app" / "local-peer-certs"
    count = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
                count += 1
        except OSError:
            continue
    return count


def _guarded(label: str, action: Callable[[], Any]) -> bool:
    try:
        action()
        return True
    except Exception as error:  # noqa: BLE001 - one failed side effect must not abort provisioning
        _log(f"Swarm cleanup step '{label}' did not complete: {error.__class__.__name__}")
        return False


def _revoke_shared_credentials(settings: Any) -> List[str]:
    """Drop credentials imported from the old swarm and stop sharing our own with it."""
    incomplete: List[str] = []
    try:
        from ..device import smtp_manager, tailnet_service, vpn_manager
    except ImportError:  # pragma: no cover - direct script execution fallback
        from device import smtp_manager, tailnet_service, vpn_manager  # type: ignore

    def vpn_step() -> None:
        state = vpn_manager._load_state(settings)
        if state["source_peer_id"]:
            vpn_manager._revoke_local_credentials(settings, CLONE_REVOCATION_REASON)
        elif state["sharing_enabled"]:
            vpn_manager._save_state(settings, sharing_enabled=False)

    def smtp_step() -> None:
        state = smtp_manager._load_state(settings)
        if state["source_peer_id"]:
            smtp_manager._revoke_local_credentials(settings, CLONE_REVOCATION_REASON)
        elif state["sharing_enabled"]:
            smtp_manager._save_state(settings, sharing_enabled=False)

    def tailnet_step() -> None:
        # The stored enrollment key belongs to the source Drone's tailnet node; a clone must
        # not be able to silently re-enroll with (or re-share) it.
        tailnet_service._revoke_local_sharing(settings, CLONE_REVOCATION_REASON)

    for label, step in (("vpn", vpn_step), ("smtp", smtp_step), ("tailnet-key", tailnet_step)):
        if not _guarded(label, step):
            incomplete.append(label)
    return incomplete


def reset_swarm_identity_state(settings: Any) -> Dict[str, Any]:
    """Clear inherited swarm trust. Raises if the SQLite reset itself fails."""
    removed = _reset_database(settings)
    pins = _clear_pinned_peer_certificates(settings)
    incomplete = _revoke_shared_credentials(settings)
    _log(
        "Inherited swarm trust cleared "
        f"(peers={removed.get('local_paired_peers', 0)}, pinned_certificates={pins}, "
        f"membership_rows={removed.get('swarm_membership', 0)})"
    )
    return {"removed": removed, "pinned_certificates_removed": pins, "incomplete": incomplete}
