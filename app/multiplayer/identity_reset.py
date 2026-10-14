"""Drop multiplayer state that belongs to the swarm a cloned disk came from.

Called from ``transfer.swarm_reset`` when a clone gets a fresh identity. Sessions,
invitations, history and the replay window name peers of the original Drone, the
invite allow-list holds its peer ids, and compatibility reports imported from
those peers carry their attribution -- none of that is valid for the new device.
Emulator settings the original had changed for a session are put back first.
Locally confirmed compatibility records and the user's own settings are kept.
Nothing here announces anything to any peer.
"""

from typing import Any, Dict

from .compat_db import CompatibilityDatabase
from .config_guard import ConfigGuard
from .persistence import Document
from .settings_store import MultiplayerSettings


def reset_multiplayer_identity_state(settings: Any) -> Dict[str, int]:
    result = {"restored_sessions": 0, "documents_cleared": 0, "imported_reports_removed": 0}
    guard = ConfigGuard(settings)
    for session_id in list(guard.pending()):
        guard.restore(session_id)
        result["restored_sessions"] += 1
    for key, empty in (("sessions", dict), ("invitations", dict), ("history", list), ("replay", dict)):
        Document(settings, key, empty).save(empty())
        result["documents_cleared"] += 1
    store = MultiplayerSettings(settings)
    if store.get()["privacy"]["allowed_inviters"]:
        store.update({"privacy": {"allowed_inviters": []}})

    def drop_imported(doc: dict) -> None:
        records = doc.get("records") or {}
        for name in [name for name, record in records.items() if str(record.get("source", "")).startswith("peer:")]:
            records.pop(name)
            result["imported_reports_removed"] += 1
    CompatibilityDatabase(settings)._doc.update(drop_imported)
    return result
