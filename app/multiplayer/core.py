"""Shared plumbing of the multiplayer orchestrator (identity, peers, audit, config, workers).

``MultiplayerOrchestrator`` (``orchestrator.py``) is composed from mixins that
all build on this base, mirroring how ``RomRepository`` and the request handler
are split elsewhere in the Drone.
"""

import socket
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import models as m
from .adapters import adapters_by_id, build_adapters
from .compat_db import CompatibilityDatabase
from .config_guard import ConfigGuard
from .environment import SystemProbe
from .launcher import GameLauncher
from .matching import GameAssessor, GameLibrary
from .network import NetworkService
from .settings_store import MultiplayerSettings
from .store import SessionStore

try:
    from ..device import notifications as _notifications
    from ..transfer import local_network as _local_network
except ImportError:  # pragma: no cover - flat execution
    from device import notifications as _notifications  # type: ignore
    from transfer import local_network as _local_network  # type: ignore

# Audit-trail kinds (no SMTP toggle; see device.notifications.MULTIPLAYER_EVENT_TYPES).
EVENT_INVITATION = "multiplayer_invitation"
EVENT_SESSION = "multiplayer_session"


def _spawn_thread(fn: Callable[[], None], name: str) -> None:
    threading.Thread(target=fn, name=name, daemon=True).start()


class OrchestratorBase:
    def __init__(self, settings: Any, repository: Any, *, probe: Optional[SystemProbe] = None,
                 adapters: Optional[list] = None, store: Optional[SessionStore] = None,
                 config_store: Optional[MultiplayerSettings] = None, compat: Optional[CompatibilityDatabase] = None,
                 guard: Optional[ConfigGuard] = None, network: Optional[NetworkService] = None,
                 launcher: Optional[GameLauncher] = None, library: Optional[GameLibrary] = None,
                 paired_peers: Optional[Callable[[], List[dict]]] = None,
                 peer_checks: Optional[Callable[[], List[dict]]] = None,
                 spawn: Callable[[Callable[[], None], str], None] = _spawn_thread,
                 audit: Optional[Callable[..., Any]] = None, bell: Optional[Callable[..., Any]] = None,
                 sleep: Callable[[float], None] = None) -> None:
        import time as _time
        self.settings = settings
        self.repository = repository
        self.probe = probe or SystemProbe(settings)
        self.adapters = adapters if adapters is not None else build_adapters(self.probe)
        self.adapter_map: Dict[str, Any] = adapters_by_id(self.adapters)
        self.store = store or SessionStore(settings)
        self.cfg = config_store or MultiplayerSettings(settings)
        self.compat = compat or CompatibilityDatabase(settings)
        self.guard = guard or ConfigGuard(settings)
        self.network = network or NetworkService(settings)
        self.launcher = launcher or GameLauncher()
        self.library = library or GameLibrary(settings, repository)
        self.assessor = GameAssessor(self.adapters, self.compat, lambda: self.cfg.get()["emulators"],
                                     cached_hash=lambda game: self.library.cached_content_hash(game))
        self._paired_peers_fn = paired_peers or (lambda: _local_network.paired_peers(settings))
        self._peer_checks_fn = peer_checks or (lambda: _local_network.load_peer_checks(settings))
        self._spawn = spawn
        self._audit_fn = audit or _notifications.record_audit_event
        self._bell_fn = bell or _notifications.record_event
        self._sleep = sleep or _time.sleep
        self._locks: Dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._creation_lock = threading.RLock()
        self._match_cache: Dict[Any, tuple] = {}

    # ----------------------------------------------------------------- identity
    @property
    def me(self) -> str:
        return str(self.settings.device_id)

    @property
    def my_name(self) -> str:
        return m.clean_text(getattr(self.settings, "hostname_override", None) or socket.gethostname() or self.me, 80)

    def session_lock(self, session_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.RLock())

    # --------------------------------------------------------------------- gates
    def require_enabled(self) -> dict:
        config = self.cfg.get()
        if not config["general"]["enabled"]:
            raise m.MultiplayerError("multiplayer_disabled", "Multiplayer is turned off in Multiplayer settings.", 403)
        return config

    # --------------------------------------------------------------------- peers
    def preferred_transport(self) -> str:
        config = self.cfg.get()["networking"]
        return "tailnet" if config["transport"] == "auto" and config["prefer_tailscale"] else config["transport"]

    def paired_peers(self) -> List[dict]:
        try:
            return list(self._paired_peers_fn())
        except Exception:  # noqa: BLE001 - a corrupt peer table must not take the page down
            return []

    def paired_peer(self, peer_id: str) -> dict:
        peer_id = m.clean_peer_id(peer_id)
        for peer in self.paired_peers():
            if str(peer.get("drone_id") or peer.get("device_id") or "") == peer_id:
                return peer
        raise m.MultiplayerError("not_paired", "That Drone is not a paired member of your swarm. Pair with it first.", 403)

    def peer_name(self, peer: dict) -> str:
        return m.clean_text(peer.get("name") or peer.get("hostname") or peer.get("drone_id"), 80)

    def peer_health(self) -> Dict[str, dict]:
        latest: Dict[str, dict] = {}
        try:
            checks = self._peer_checks_fn()
        except Exception:  # noqa: BLE001
            checks = []
        for check in checks:
            target = str(check.get("target_drone_id") or "")
            if target and str(check.get("checked_at") or "") >= str(latest.get(target, {}).get("checked_at") or ""):
                latest[target] = check
        return latest

    # -------------------------------------------------------------------- audit
    def audit(self, kind: str, title: str, message: str = "", details: Optional[dict] = None, *, bell: bool = False) -> None:
        """Auditable session events. Never carries the session credential."""
        if not self.cfg.get()["privacy"]["session_logging"] and not bell:
            return
        safe = {key: value for key, value in (details or {}).items() if key not in ("secret", "password")}
        try:
            (self._bell_fn if bell else self._audit_fn)(self.settings, kind, title, message, safe)
        except Exception:  # noqa: BLE001 - auditing must never break a session
            pass

    # ------------------------------------------------------------------- config
    def apply_edits(self, session_id: str, edits: List[dict]) -> None:
        grouped: Dict[tuple, list] = {}
        for edit in edits:
            grouped.setdefault((edit["path"], edit["kind"], edit.get("watch") is True), []).append(edit)
        for (path, kind, watch), items in grouped.items():
            apply = self.guard.watch_generated if watch else self.guard.apply
            apply(session_id, Path(str(path)), kind,
                             [{"key": item["key"], "section": item.get("section", ""), "value": item["value"]} for item in items])

    def restore_if_enabled(self, session_id: str) -> dict:
        if not self.cfg.get()["general"]["auto_restore_configuration"]:
            return {"restored": 0, "left_alone": 0, "errors": [], "skipped": "auto restore is off"}
        return self.guard.restore(session_id)

    # ---------------------------------------------------------------- game files
    def game_query(self, game: dict) -> dict:
        return {"system": game["system"], "rom_name": game.get("rom_name", ""), "title_key": m.normalize_title(game.get("rom_name", "")),
                "fingerprint": game.get("fingerprint", ""), "size": game.get("size", 0)}

    def local_copy(self, game: dict) -> Optional[dict]:
        found = self.library.find_copy(self.game_query(game))
        return found

    def controller_count(self) -> int:
        try:
            return len(self.probe.controllers())
        except Exception:  # noqa: BLE001
            return 0
