"""Small JSON documents in the Drone's shared SQLite state DB (``app_state`` table).

Multiplayer persists only what recovery needs (sessions, invitations, settings,
compatibility records, the replay window). Schema is never altered in place:
documents are free-form JSON under the ``multiplayer`` namespace.
"""

import threading
from typing import Any, Callable

try:
    from ..storage.state_store import database_path, load_payload, save_payload
except ImportError:  # pragma: no cover - flat execution
    from storage.state_store import database_path, load_payload, save_payload  # type: ignore

NAMESPACE = "multiplayer"

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def lock_for(settings: Any, key: str) -> threading.RLock:
    token = (str(database_path(settings.userdata_root)), key)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(token)
        if lock is None:
            lock = _LOCKS[token] = threading.RLock()
        return lock


class Document:
    """One JSON document with an in-process lock for read-modify-write."""

    def __init__(self, settings: Any, key: str, default: Callable[[], Any]) -> None:
        self.settings = settings
        self.key = key
        self._default = default
        self.lock = lock_for(settings, key)

    @property
    def _db(self):
        return database_path(self.settings.userdata_root)

    def load(self) -> Any:
        value = load_payload(self._db, NAMESPACE, None, state_key=self.key)
        fresh = self._default()
        if value is None or type(value) is not type(fresh):
            return fresh
        return value

    def save(self, value: Any) -> None:
        save_payload(self._db, NAMESPACE, value, state_key=self.key)

    def update(self, mutate: Callable[[Any], Any]) -> Any:
        with self.lock:
            value = self.load()
            result = mutate(value)
            self.save(value)
            return result
