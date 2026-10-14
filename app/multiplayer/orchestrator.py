"""The Multiplayer Orchestrator: one object per Drone, composed from focused mixins.

* ``core``      identity, peers, auditing, configuration application
* ``sessions``  host-side session creation, invitations, lobby control, views
* ``protocol``  authenticated peer messages and the guest's local actions
* ``lifecycle`` start/launch coordination, health, termination, recovery
* ``library``   dashboard, game browser, adapters, settings (read side)

There is no central coordinator: any trusted peer can host, and each Drone only
ever changes its own state in response to an authenticated, explicit action.
"""

import threading
from typing import Any, Optional

from .core import OrchestratorBase
from .library import LibraryMixin
from .lifecycle import LifecycleMixin
from .protocol import ProtocolMixin
from .sessions import SessionMixin


class MultiplayerOrchestrator(LibraryMixin, LifecycleMixin, ProtocolMixin, SessionMixin, OrchestratorBase):
    pass


_INSTANCE: Optional[MultiplayerOrchestrator] = None
_INSTANCE_KEY: Optional[tuple] = None
_LOCK = threading.Lock()


def get_orchestrator(settings: Any, repository: Any) -> MultiplayerOrchestrator:
    """Process-wide orchestrator for these settings (rebuilt if the Drone's settings object changes)."""
    global _INSTANCE, _INSTANCE_KEY
    key = (id(settings), str(settings.userdata_root), id(repository))
    with _LOCK:
        if _INSTANCE is None or _INSTANCE_KEY != key:
            _INSTANCE = MultiplayerOrchestrator(settings, repository)
            _INSTANCE_KEY = key
        return _INSTANCE
