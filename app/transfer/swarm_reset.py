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
from typing import Any, Callable, Dict, List, Optional

try:
    from ..storage.state_store import database_path, open_database
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path, open_database  # type: ignore

# app_state namespaces that must not be inherited by a newly provisioned identity.
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
    # This local API credential grants admin-level MCP access to its issuing Drone.
    "mcp",
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
    with open_database(database_path(settings.userdata_root)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for namespace in IDENTITY_BOUND_NAMESPACES:
            cursor = connection.execute("DELETE FROM app_state WHERE namespace = ?", (namespace,))
            removed[namespace] = max(0, cursor.rowcount or 0)
        for namespace in IDENTITY_BOUND_EVENT_NAMESPACES:
            cursor = connection.execute("DELETE FROM app_events WHERE namespace = ?", (namespace,))
            removed[namespace] = max(0, cursor.rowcount or 0)
        cursor = connection.execute("DELETE FROM peer_route_cache")
        removed["peer_route_cache"] = max(0, cursor.rowcount or 0)
        # Browser sessions belong to the source installation. A new identity must
        # require a fresh login even though the configured credentials survive.
        cursor = connection.execute("DELETE FROM sessions")
        removed["sessions"] = max(0, cursor.rowcount or 0)
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
    """Remove every pinned peer certificate; raises if any cannot be removed.

    A pin that survives would keep trusting an inherited peer, so the caller must not
    treat provisioning as complete (the journal makes the next start retry).
    """
    try:
        from ..common.device_identity import _is_mock_path, _require_real_path
    except ImportError:  # pragma: no cover - direct script execution fallback
        from common.device_identity import _is_mock_path, _require_real_path  # type: ignore
    if _is_mock_path(settings.userdata_root):
        return 0
    _require_real_path(settings.userdata_root, "userdata_root")
    directory = Path(settings.userdata_root) / "system" / "drone-app" / "local-peer-certs"
    count = 0
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return 0
    failure: Optional[OSError] = None
    for entry in entries:
        try:
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
                count += 1
        except FileNotFoundError:
            continue
        except OSError as error:
            failure = failure or error
    if failure is not None:
        raise failure
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


def revoke_inherited_access(settings: Any) -> None:
    """Invalidate browser sessions and the MCP token copied from the source installation.

    Used when provisioning of a clone is held: the full reset may never run, so the
    inherited admin session and MCP bearer must be dropped here without touching peers,
    pins or credentials. Each credential is revoked independently, and a blocked DELETE
    falls back to neutralising the row in place, so one failure cannot leave the other
    (or the same) credential usable. Raises only if some access could not be revoked.
    """
    db_path = database_path(settings.userdata_root)
    steps = (
        ("sessions", "DELETE FROM sessions", (),
         "UPDATE sessions SET expires_at = ?", ("1970-01-01T00:00:00+00:00",)),
        ("mcp", "DELETE FROM app_state WHERE namespace = ?", ("mcp",),
         "UPDATE app_state SET payload = ? WHERE namespace = ?", ("{}", "mcp")),
    )
    failure: Optional[Exception] = None
    for label, delete_sql, delete_args, neutralise_sql, neutralise_args in steps:
        for sql, args in ((delete_sql, delete_args), (neutralise_sql, neutralise_args)):
            try:
                with open_database(db_path) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(sql, args)
                    connection.commit()
                break
            except Exception as error:  # noqa: BLE001 - try the fallback, then report
                _log(f"Inherited {label} revocation step failed: {error.__class__.__name__}")
                last = error
        else:
            failure = failure or last
    if failure is not None:
        raise failure


def reset_swarm_identity_state(settings: Any) -> Dict[str, Any]:
    """Clear inherited swarm trust. Raises if the SQLite reset itself fails."""
    try:
        from ..device import nfs_export_manager
    except ImportError:  # pragma: no cover - direct script execution fallback
        from device import nfs_export_manager  # type: ignore
    # Kernel exports survive a Drone process restart. Retain ownership records
    # until every client is unexported and the Drone bind mounts are gone.
    nfs_export_manager.revoke_exports_for_identity_change(settings)
    removed = _reset_database(settings)
    pins = _clear_pinned_peer_certificates(settings)
    incomplete = _revoke_shared_credentials(settings)
    _log(
        "Inherited swarm trust cleared "
        f"(peers={removed.get('local_paired_peers', 0)}, pinned_certificates={pins}, "
        f"membership_rows={removed.get('swarm_membership', 0)})"
    )
    return {"removed": removed, "pinned_certificates_removed": pins, "incomplete": incomplete}
