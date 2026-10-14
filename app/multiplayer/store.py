"""Session, invitation, history and replay-window persistence.

The host is the single authority for a session. Every host-side mutation goes
through ``SessionStore.mutate`` which enforces the state machine and bumps a
monotonically increasing ``version``; guests hold read-only replicas that only
move forward (``apply_replica``), so stale or duplicated updates cannot
overwrite newer state. Peer-supplied snapshots are rebuilt field by field
(``sanitize_session``) -- a raw peer dict is never stored.
"""

import copy
import json
import re
from datetime import timedelta
from typing import Any, Callable, List, Optional

from . import models as m
from .persistence import Document

HISTORY_LIMIT = 100
REPLAY_LIMIT = 500  # per authenticated caller; never evict unexpired entries
REPLAY_WINDOW = timedelta(minutes=15)
MAX_PLAYERS_IN_SNAPSHOT = m.MAX_PLAYERS_LIMIT + 4
LOCAL_KEYS = ("local",)


def _hex_or_empty(value: Any, limit: int = 64) -> str:
    text = str(value or "").strip().lower()
    return text if text and len(text) <= limit and all(c in "0123456789abcdef" for c in text) else ""


def sanitize_game(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    try:
        size = max(0, int(raw.get("size") or 0))
    except (TypeError, ValueError, OverflowError):
        size = 0
    game = {
        "system": m.clean_system(raw.get("system")),
        "title": m.clean_text(raw.get("title") or raw.get("rom_name"), m.MAX_TITLE),
        "rom_name": m.clean_text(raw.get("rom_name"), 255),
        "fingerprint": _hex_or_empty(raw.get("fingerprint")),
        "sha256": _hex_or_empty(raw.get("sha256")),
        "size": size,
        "unique_id": "",
        "game_id": re.sub(r"[^A-Za-z0-9]", "", str(raw.get("game_id") or ""))[:16],
        "players": raw.get("players") if isinstance(raw.get("players"), int) and not isinstance(raw.get("players"), bool)
        and 1 <= raw.get("players") <= 16 else None,
    }
    if raw.get("unique_id"):
        game["unique_id"] = m.clean_unique_id(raw.get("unique_id"))
    if not game["rom_name"]:
        raise m.MultiplayerError("invalid_game", "game.rom_name is required")
    return game


def sanitize_emulator(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "adapter": m.clean_token(raw.get("adapter"), "emulator adapter"),
        "name": m.clean_token(raw.get("name"), "emulator name"),
        "core": m.clean_token(raw.get("core"), "emulator core"),
        "version": m.clean_version(raw.get("version")),
        "core_version": m.clean_version(raw.get("core_version")),
    }


def sanitize_stream_profile(raw: Any) -> dict:
    """Fixed-shape stream recommendation; anything else a peer sends is dropped."""
    if not isinstance(raw, dict) or not raw:
        return {}

    def number(value: Any, low: int, high: int) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high else 0
    return {
        "resolution": re.sub(r"[^0-9x]", "", str(raw.get("resolution") or ""))[:12],
        "fps": number(raw.get("fps"), 1, 240),
        "bitrate_kbps": number(raw.get("bitrate_kbps"), 1, 1000000),
        "encoder": "hardware" if raw.get("encoder") == "hardware" else "software",
        "hardware_encoder_available": raw.get("hardware_encoder_available") is True,
        "note": m.clean_text(raw.get("note"), 200),
    }


def sanitize_player(raw: Any, peer_id: str) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    status = str(raw.get("status") or m.P_INVITED)
    if status not in (m.P_INVITED, m.P_JOINED, m.P_DECLINED, m.P_LEFT, m.P_REMOVED, m.P_LAUNCHING, m.P_RUNNING,
                      m.P_FAILED, m.P_UNREACHABLE):
        status = m.P_INVITED
    slot = raw.get("slot")
    slot = slot if isinstance(slot, int) and not isinstance(slot, bool) and 1 <= slot <= m.MAX_PLAYERS_LIMIT else None
    latency = raw.get("latency_ms")
    latency = round(float(latency), 1) if isinstance(latency, (int, float)) and not isinstance(latency, bool) and 0 <= latency < 600000 else None
    controllers = raw.get("controllers")
    controllers = controllers if isinstance(controllers, int) and not isinstance(controllers, bool) and 0 <= controllers <= 64 else None
    compat = raw.get("compat") if isinstance(raw.get("compat"), dict) else {}
    cls = str(compat.get("classification") or m.UNKNOWN)
    return {
        "device_id": peer_id,
        "name": m.clean_text(raw.get("name") or peer_id, 80),
        "role": m.HOST_ROLE if raw.get("role") == m.HOST_ROLE else m.GUEST_ROLE,
        "status": status,
        "ready": raw.get("ready") is True,
        "slot": slot,
        "latency_ms": latency,
        "route": m.clean_text(raw.get("route"), 16),
        "controllers": controllers,
        "compat": {
            "classification": cls if cls in m.COMPATIBILITY_CLASSES else m.UNKNOWN,
            "reasons": [m.clean_text(item, 300) for item in (compat.get("reasons") or [])[:12]] if isinstance(compat.get("reasons"), list) else [],
        },
        "invited_at": m.clean_text(raw.get("invited_at"), 40),
        "joined_at": m.clean_text(raw.get("joined_at"), 40),
        "error": m.clean_text(raw.get("error"), 300),
    }


def sanitize_session(raw: Any) -> dict:
    """Rebuild a session snapshot from an untrusted dict. Raises MultiplayerError."""
    raw = m.require_dict(raw, "session")
    state = str(raw.get("state") or "")
    if state not in m.SESSION_STATES:
        raise m.MultiplayerError("invalid_state", "session state is not recognised")
    try:
        version = int(raw.get("version"))
    except (TypeError, ValueError, OverflowError):
        raise m.MultiplayerError("invalid_version", "session version must be an integer") from None
    if not 0 <= version < 2 ** 31:
        raise m.MultiplayerError("invalid_version", "session version is out of range")
    players_raw = raw.get("players") if isinstance(raw.get("players"), dict) else {}
    if len(players_raw) > MAX_PLAYERS_IN_SNAPSHOT:
        raise m.MultiplayerError("too_many_players", "session lists too many players")
    players = {}
    for key, value in players_raw.items():
        peer_id = m.clean_peer_id(key)
        players[peer_id] = sanitize_player(value, peer_id)
    events = []
    for item in (raw.get("events") or [])[-m.MAX_EVENTS:] if isinstance(raw.get("events"), list) else []:
        if isinstance(item, dict):
            events.append({"at": m.clean_text(item.get("at"), 40), "kind": m.clean_text(item.get("kind"), 40),
                           "message": m.clean_text(item.get("message"), 300), "actor": m.clean_text(item.get("actor"), 128)})
    mechanism = m.clean_mechanism(raw.get("mechanism"), allow_empty=True)
    return {
        "id": m.clean_session_id(raw.get("id")),
        "version": version,
        "state": state,
        "host_id": m.clean_peer_id(raw.get("host_id"), "host id"),
        "host_name": m.clean_text(raw.get("host_name"), 80),
        "created_at": m.clean_text(raw.get("created_at"), 40),
        "updated_at": m.clean_text(raw.get("updated_at"), 40),
        "expires_at": m.clean_text(raw.get("expires_at"), 40),
        "game": sanitize_game(raw.get("game")),
        "mechanism": mechanism,
        "mechanism_reason": m.clean_text(raw.get("mechanism_reason"), 500),
        "emulator": sanitize_emulator(raw.get("emulator")),
        "max_players": m.clean_int(raw.get("max_players", 2), "max_players", 2, m.MAX_PLAYERS_LIMIT),
        "players": players,
        "events": events,
        "error": m.clean_text(raw.get("error"), 400),
        "manual_steps": [m.clean_text(item, 300) for item in (raw.get("manual_steps") or [])[:12]] if isinstance(raw.get("manual_steps"), list) else [],
        "stream_profile": sanitize_stream_profile(raw.get("stream_profile")),
    }


def require_same_identity(expected: dict, incoming: dict) -> None:
    """Bind all later snapshots to the exact session the local user reviewed."""
    for field in ("id", "host_id", "mechanism", "max_players"):
        if expected.get(field) != incoming.get(field):
            raise m.MultiplayerError("session_identity_changed", "The host changed the session you agreed to join.", 409)
    for group, fields in (("game", ("system", "rom_name", "fingerprint", "sha256", "size", "game_id")),
                          ("emulator", ("adapter", "name", "core", "version", "core_version"))):
        for field in fields:
            if expected.get(group, {}).get(field, "") != incoming.get(group, {}).get(field, ""):
                raise m.MultiplayerError("session_identity_changed", "The host changed the game or emulator you agreed to join.", 409)


def public_view(session: dict, *, viewer_id: str = "") -> dict:
    """What the browser (and replicas) may see: never the join credential or local plans."""
    view = copy.deepcopy({key: value for key, value in session.items() if key not in ("secret", "local")})
    local = session.get("local") if isinstance(session.get("local"), dict) else {}
    view["local_role"] = local.get("role") or ("host" if session.get("host_id") == viewer_id else "guest")
    pending = local.get("pending_launch")
    view["pending_launch"] = bool(pending) and isinstance(pending, dict) and not pending.get("consumed")
    view["is_host"] = view["local_role"] == "host"
    return view


def replica_view(session: dict) -> dict:
    """Snapshot pushed to guests (no secret, no host-local state, no host-only stream profile detail)."""
    return copy.deepcopy({key: value for key, value in session.items() if key not in ("secret", "local")})


def add_event(session: dict, kind: str, message: str, actor: str = "") -> None:
    events = session.setdefault("events", [])
    events.append({"at": m.now_iso(), "kind": m.clean_text(kind, 40), "message": m.clean_text(message, 300),
                   "actor": m.clean_text(actor, 128)})
    del events[:-m.MAX_EVENTS]


class SessionStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._sessions = Document(settings, "sessions", dict)
        self._invitations = Document(settings, "invitations", dict)
        self._history = Document(settings, "history", list)
        self._replay = Document(settings, "replay", dict)

    # ---------------------------------------------------------------- sessions
    def get(self, session_id: str) -> Optional[dict]:
        return self._sessions.load().get(session_id)

    def require(self, session_id: str) -> dict:
        session = self.get(m.clean_session_id(session_id))
        if session is None:
            raise m.MultiplayerError("session_not_found", "No such multiplayer session on this Drone", 404)
        return session

    def all(self) -> List[dict]:
        return sorted(self._sessions.load().values(), key=lambda item: item.get("created_at", ""), reverse=True)

    def active(self) -> List[dict]:
        return [item for item in self.all() if item.get("state") not in m.TERMINAL_STATES and not (item.get("local") or {}).get("ended")]

    def create(self, session: dict) -> dict:
        def add(store: dict) -> dict:
            if session["id"] in store:
                raise m.MultiplayerError("session_exists", "A session with this id already exists", 409)
            store[session["id"]] = session
            return session
        return self._sessions.update(add)

    def mutate(self, session_id: str, fn: Callable[[dict], Any], *, expected_version: Optional[int] = None) -> dict:
        """Apply ``fn`` to a copy, validate the transition, bump the version if anything changed."""
        def apply(store: dict) -> dict:
            current = store.get(session_id)
            if current is None:
                raise m.MultiplayerError("session_not_found", "No such multiplayer session on this Drone", 404)
            if expected_version is not None and int(current.get("version", 0)) != int(expected_version):
                raise m.MultiplayerError("version_conflict", "The session changed; refresh and try again", 409,
                                         {"current_version": current.get("version")})
            working = copy.deepcopy(current)
            before = json.dumps(working, sort_keys=True, default=str)
            fn(working)
            if json.dumps(working, sort_keys=True, default=str) == before:
                return current
            if working.get("state") != current.get("state") and not m.can_transition(current["state"], working["state"]):
                raise m.MultiplayerError(
                    "illegal_transition",
                    f"A session cannot move from {current['state']} to {working['state']}", 409,
                    {"from": current["state"], "to": working["state"]})
            working["version"] = int(current.get("version", 0)) + 1
            working["updated_at"] = m.now_iso()
            store[session_id] = working
            return working
        return self._sessions.update(apply)

    def apply_replica(self, snapshot: dict, caller_id: str) -> Optional[dict]:
        """Guest side: accept a host snapshot only if it is from the host and strictly newer.

        Returns the stored session, or ``None`` for a stale/duplicate update.
        """
        clean = sanitize_session(snapshot)
        if clean["host_id"] != caller_id:
            raise m.MultiplayerError("not_session_host", "Only the session host can update a session", 403)

        def apply(store: dict) -> Optional[dict]:
            current = store.get(clean["id"])
            if current is None:
                raise m.MultiplayerError("session_not_found", "This Drone is not part of that session", 404)
            if current.get("host_id") != caller_id:
                raise m.MultiplayerError("not_session_host", "Only the session host can update a session", 403)
            if (current.get("local") or {}).get("role") == m.HOST_ROLE:
                raise m.MultiplayerError("not_replica", "This Drone hosts that session", 409)
            if clean["version"] <= int(current.get("version", 0)):
                return None
            require_same_identity(current, clean)
            if current["state"] == m.COMPLETED and clean["state"] != m.COMPLETED:
                raise m.MultiplayerError("session_closed", "A completed session cannot be reopened.", 409)
            clean["local"] = current.get("local", {})
            store[clean["id"]] = clean
            return clean
        return self._sessions.update(apply)

    def adopt_replica(self, snapshot: dict, caller_id: str, local: dict) -> dict:
        """Create the guest replica when an invitation is accepted (membership already checked)."""
        clean = sanitize_session(snapshot)
        if clean["host_id"] != caller_id:
            raise m.MultiplayerError("not_session_host", "Only the session host can update a session", 403)
        clean["local"] = dict(local)

        def put(store: dict) -> dict:
            existing = store.get(clean["id"])
            if existing is not None:
                if existing.get("host_id") != caller_id or (existing.get("local") or {}).get("role") == m.HOST_ROLE:
                    raise m.MultiplayerError("session_exists", "That session id already belongs to another session.", 409)
                require_same_identity(existing, clean)
                if int(existing.get("version", 0)) >= clean["version"]:
                    return existing
                clean["local"] = {**existing.get("local", {}), **clean["local"]}
            store[clean["id"]] = clean
            return clean
        return self._sessions.update(put)

    def set_local(self, session_id: str, **values: Any) -> None:
        """Update host/guest-local bookkeeping that is never replicated (no version bump)."""
        def apply(store: dict) -> None:
            session = store.get(session_id)
            if session is not None:
                local = dict(session.get("local") or {})
                local.update(values)
                session["local"] = local
        self._sessions.update(apply)

    def delete(self, session_id: str) -> None:
        self._sessions.update(lambda store: store.pop(session_id, None))

    # ------------------------------------------------------------- invitations
    def invitation(self, session_id: str) -> Optional[dict]:
        return self._invitations.load().get(session_id)

    def invitations(self) -> List[dict]:
        return sorted(self._invitations.load().values(), key=lambda item: item.get("received_at", ""), reverse=True)

    def put_invitation(self, invitation: dict) -> None:
        def put(store: dict) -> None:
            store[invitation["session_id"]] = invitation
            # Bound the table: drop the oldest resolved entries first.
            if len(store) > 200:
                ordered = sorted(store.values(), key=lambda item: (item.get("status") == "pending", item.get("received_at", "")))
                for stale in ordered[: len(store) - 200]:
                    store.pop(stale["session_id"], None)
        self._invitations.update(put)

    def update_invitation(self, session_id: str, **values: Any) -> Optional[dict]:
        def apply(store: dict) -> Optional[dict]:
            item = store.get(session_id)
            if item is not None:
                item.update(values)
            return item
        return self._invitations.update(apply)

    # ----------------------------------------------------------------- history
    def archive(self, session: dict, outcome: str) -> None:
        entry = {
            "id": session["id"], "title": session.get("game", {}).get("title", ""),
            "system": session.get("game", {}).get("system", ""), "host_id": session.get("host_id", ""),
            "host_name": session.get("host_name", ""), "mechanism": session.get("mechanism", ""),
            "emulator": session.get("emulator", {}), "outcome": m.clean_text(outcome, 40),
            "players": sorted(session.get("players", {})), "created_at": session.get("created_at", ""),
            "ended_at": m.now_iso(), "error": session.get("error", ""),
        }

        def add(history: list) -> None:
            history[:] = [item for item in history if item.get("id") != entry["id"]]
            history.insert(0, entry)
            del history[HISTORY_LIMIT:]
        self._history.update(add)

    def history(self, limit: int = 50) -> List[dict]:
        return self._history.load()[: max(1, min(int(limit), HISTORY_LIMIT))]

    # ------------------------------------------------------------ replay guard
    def check_replay(self, caller_id: str, message_id: str, issued_at: Any) -> str:
        """Return ``"ok"``, ``"duplicate"`` or raise for a stale/future-dated message."""
        issued = m.parse_time(issued_at)
        if issued is None:
            raise m.MultiplayerError("invalid_timestamp", "issued_at must be an ISO-8601 timestamp")
        current = m.now()
        if abs(current - issued) > REPLAY_WINDOW:
            raise m.MultiplayerError("stale_message", "The message is outside the accepted time window", 400)
        key = f"{caller_id}:{message_id}"

        def record(seen: dict) -> str:
            cutoff = (current - REPLAY_WINDOW).isoformat()
            for stale in [name for name, stamp in seen.items() if stamp < cutoff]:
                seen.pop(stale, None)
            if key in seen:
                return "duplicate"
            if sum(name.startswith(f"{caller_id}:") for name in seen) >= REPLAY_LIMIT or len(seen) >= REPLAY_LIMIT * 20:
                raise m.MultiplayerError("message_rate_limit", "Too many multiplayer messages in the replay window. Try again later.", 429)
            seen[key] = current.isoformat()
            return "ok"
        return self._replay.update(record)
