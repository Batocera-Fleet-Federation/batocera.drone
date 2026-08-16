"""Central entry point for recording a Drone activity event.

Deliberately a dependency-free "leaf" module -- it only imports
``storage/audit_store.py``, nothing from ``transfer/`` or the rest of
``device/`` -- so any layer of the app (device control, automation, the
peer-transfer stack, the ROM scanner, VPN) can call ``record_event()``
without risking an import cycle.

``record_event()`` never raises: a bug in the notifications subsystem must
never break the feature that triggered it (a VPN connect, a completed
download, a torrent finishing, ...).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

try:
    from ..storage import audit_store as _audit_store
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage import audit_store as _audit_store  # type: ignore


_LOG = logging.getLogger(__name__)

# The 16 event types the SMTP notifications page exposes a toggle for. Order
# here is the order they render in on that page.
EVENT_TYPES = (
    "vpn_connected",
    "vpn_disconnected",
    "swarm_peer_connected",
    "asset_added",
    "asset_removed",
    "manual_control_submitted",
    "automation_updated",
    "asset_uploaded",
    "asset_downloaded",
    "torrent_completed",
    "drone_updated",
    "config_backup_applied",
    "torrent_move_started",
    "torrent_move_resuming",
    "torrent_move_failed",
    "torrent_move_finished",
)

EVENT_TYPE_LABELS = {
    "vpn_connected": "VPN connected",
    "vpn_disconnected": "VPN disconnected",
    "swarm_peer_connected": "Newly connected to swarm",
    "asset_added": "Asset added to a system",
    "asset_removed": "Asset removed from a system",
    "manual_control_submitted": "Manual control submitted",
    "automation_updated": "Automation setting updated",
    "asset_uploaded": "Asset uploaded (served to a peer)",
    "asset_downloaded": "Asset downloaded (pulled from a peer)",
    "torrent_completed": "Torrent download completed",
    "drone_updated": "Drone app updated",
    "config_backup_applied": "Config backup applied to this machine",
    "torrent_move_started": "Moving downloaded torrent files started",
    "torrent_move_resuming": "Moving downloaded torrent files resumed after interruption",
    "torrent_move_failed": "Moving downloaded torrent files failed",
    "torrent_move_finished": "Moving downloaded torrent files finished",
}


def record_event(
    settings: Any,
    event_type: str,
    title: str,
    message: str = "",
    details: Optional[dict] = None,
) -> Optional[dict]:
    """Log ``event_type`` to the audit trail and the notifications inbox.

    Returns the created notification dict, or ``None`` if recording failed --
    callers should not depend on the return value for anything but an
    optional log line; this must never raise.
    """
    if event_type not in EVENT_TYPES:
        _LOG.warning("record_event called with unknown event_type=%r", event_type)
    try:
        return _audit_store.insert_event(settings, event_type, title, message=message, details=details)
    except Exception:
        _LOG.exception("failed to record audit event event_type=%s", event_type)
        return None
