"""Multiplayer vocabulary: states, mechanisms, compatibility classes, validators.

Everything a peer or browser sends is untrusted; the ``clean_*`` helpers bound
and allowlist it before it reaches the session store, a config file or a
subprocess argument list.
"""

import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA_VERSION = 1


class MultiplayerError(Exception):
    """Structured, user-facing error: ``code`` is stable, ``message`` is human readable."""

    def __init__(self, code: str, message: str, status: int = 400, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def to_dict(self) -> dict:
        payload = {"error": self.message, "code": self.code}
        if self.details:
            payload["details"] = self.details
        return payload


class CapabilityError(MultiplayerError):
    """An adapter was asked for something the installed emulator build cannot do."""

    def __init__(self, message: str, code: str = "capability_unsupported", details: Optional[dict] = None) -> None:
        super().__init__(code, message, 409, details)


# --------------------------------------------------------------------- states
CREATED = "CREATED"
DISCOVERING = "DISCOVERING"
INVITING = "INVITING"
CONFIGURING = "CONFIGURING"
WAITING_FOR_PLAYERS = "WAITING_FOR_PLAYERS"
READY = "READY"
STARTING = "STARTING"
RUNNING = "RUNNING"
DEGRADED = "DEGRADED"
FAILED = "FAILED"
STOPPING = "STOPPING"
COMPLETED = "COMPLETED"

SESSION_STATES = (CREATED, DISCOVERING, INVITING, CONFIGURING, WAITING_FOR_PLAYERS, READY, STARTING, RUNNING,
                  DEGRADED, FAILED, STOPPING, COMPLETED)
PRE_START_STATES = frozenset({CREATED, DISCOVERING, INVITING, CONFIGURING, WAITING_FOR_PLAYERS, READY})
_LOBBY = PRE_START_STATES - {CREATED}
TERMINAL_STATES = frozenset({COMPLETED})
LIVE_STATES = frozenset({STARTING, RUNNING, DEGRADED})

# The lobby states are freely interchangeable (players come and go) but CREATED
# can never be re-entered, STARTING only follows READY, and COMPLETED is final.
TRANSITIONS = {
    CREATED: frozenset(_LOBBY | {STOPPING, FAILED}),
    DISCOVERING: frozenset(_LOBBY - {DISCOVERING} | {STOPPING, FAILED}),
    INVITING: frozenset(_LOBBY - {INVITING} | {STOPPING, FAILED}),
    CONFIGURING: frozenset(_LOBBY - {CONFIGURING} | {STOPPING, FAILED}),
    WAITING_FOR_PLAYERS: frozenset(_LOBBY - {WAITING_FOR_PLAYERS} | {STOPPING, FAILED}),
    READY: frozenset(_LOBBY - {READY} | {STARTING, STOPPING, FAILED}),
    STARTING: frozenset({RUNNING, DEGRADED, FAILED, STOPPING}),
    RUNNING: frozenset({DEGRADED, FAILED, STOPPING}),
    DEGRADED: frozenset({RUNNING, FAILED, STOPPING}),
    FAILED: frozenset({STOPPING, CONFIGURING}),
    STOPPING: frozenset({COMPLETED, FAILED}),
    COMPLETED: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


# ----------------------------------------------------------------- mechanisms
RETROARCH_NETPLAY = "RETROARCH_NETPLAY"
DOLPHIN_NETPLAY = "DOLPHIN_NETPLAY"
PPSSPP_ADHOC = "PPSSPP_ADHOC"
SWITCH_LDN = "SWITCH_LDN"
EMULATED_LAN = "EMULATED_LAN"
EMULATOR_NATIVE_NETWORK = "EMULATOR_NATIVE_NETWORK"
REMOTE_STREAMING = "REMOTE_STREAMING"

MECHANISMS = (RETROARCH_NETPLAY, DOLPHIN_NETPLAY, PPSSPP_ADHOC, SWITCH_LDN, EMULATED_LAN,
              EMULATOR_NATIVE_NETWORK, REMOTE_STREAMING)
MECHANISM_LABELS = {
    RETROARCH_NETPLAY: "RetroArch Netplay",
    DOLPHIN_NETPLAY: "Dolphin Netplay",
    PPSSPP_ADHOC: "PPSSPP ad hoc",
    SWITCH_LDN: "Switch LDN / LAN play",
    EMULATED_LAN: "Emulated LAN",
    EMULATOR_NATIVE_NETWORK: "Emulator-native network play",
    REMOTE_STREAMING: "Remote streaming",
}
# Native mechanisms are always preferred over streaming when both are usable.
MECHANISM_PRIORITY = (RETROARCH_NETPLAY, DOLPHIN_NETPLAY, PPSSPP_ADHOC, SWITCH_LDN, EMULATED_LAN,
                      EMULATOR_NATIVE_NETWORK, REMOTE_STREAMING)

# ------------------------------------------------------------- compatibility
VERIFIED_COMPATIBLE = "VERIFIED_COMPATIBLE"
LIKELY_COMPATIBLE = "LIKELY_COMPATIBLE"
REQUIRES_CONFIGURATION = "REQUIRES_CONFIGURATION"
EXPERIMENTAL = "EXPERIMENTAL"
INCOMPATIBLE = "INCOMPATIBLE"
UNSUPPORTED = "UNSUPPORTED"
UNKNOWN = "UNKNOWN"
COMPATIBILITY_CLASSES = (VERIFIED_COMPATIBLE, LIKELY_COMPATIBLE, REQUIRES_CONFIGURATION, EXPERIMENTAL,
                         INCOMPATIBLE, UNSUPPORTED, UNKNOWN)
# Classes a session may be started with (anything else is blocked or needs a fix first).
PLAYABLE_CLASSES = frozenset({VERIFIED_COMPATIBLE, LIKELY_COMPATIBLE, EXPERIMENTAL, REQUIRES_CONFIGURATION, UNKNOWN})

# Player lifecycle on a session.
P_INVITED, P_JOINED, P_DECLINED, P_LEFT, P_REMOVED = "invited", "joined", "declined", "left", "removed"
P_LAUNCHING, P_RUNNING, P_FAILED, P_UNREACHABLE = "launching", "running", "failed", "unreachable"
ACTIVE_PLAYER_STATUSES = frozenset({P_JOINED, P_LAUNCHING, P_RUNNING})
HOST_ROLE, GUEST_ROLE = "host", "guest"

MAX_PLAYERS_LIMIT = 8
MAX_EVENTS = 100
MAX_TITLE = 200

_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")
_MESSAGE_ID = re.compile(r"^[a-f0-9]{16,64}$")
_PEER_ID = re.compile(r"^[A-Za-z0-9_.:-]{3,128}$")
_SYSTEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
_UNIQUE_ID = re.compile(r"^[A-Za-z0-9_.:+=-]{1,128}$")
_NICKNAME_BAD = re.compile(r"[^A-Za-z0-9 _.-]")
_VERSION_TEXT = re.compile(r"[^A-Za-z0-9 ._+()\-:]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return now().isoformat()


def parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def new_session_id() -> str:
    return uuid.uuid4().hex


def new_message_id() -> str:
    return secrets.token_hex(16)


def new_secret() -> str:
    # Session credential for emulators that support a join password.
    return secrets.token_hex(12)


def clean_session_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not _SESSION_ID.fullmatch(text):
        raise MultiplayerError("invalid_session_id", "session_id must be a 32-character hex identifier")
    return text


def clean_message_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not _MESSAGE_ID.fullmatch(text):
        raise MultiplayerError("invalid_message_id", "message_id must be a hex identifier")
    return text


def clean_peer_id(value: Any, label: str = "peer id") -> str:
    text = str(value or "").strip()
    if not _PEER_ID.fullmatch(text):
        raise MultiplayerError("invalid_peer_id", f"{label} is not a valid Drone identifier")
    return text


def clean_system(value: Any) -> str:
    text = str(value or "").strip()
    if not _SYSTEM.fullmatch(text):
        raise MultiplayerError("invalid_system", "system is not a valid system identifier")
    return text.lower()


def clean_unique_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _UNIQUE_ID.fullmatch(text):
        raise MultiplayerError("invalid_game_id", "game id is not valid")
    return text


def clean_text(value: Any, limit: int = MAX_TITLE) -> str:
    """Single-line display text: control characters removed, length bounded."""
    return _CONTROL.sub(" ", str(value or "")).strip()[:limit]


_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_.+-]{0,63}$")


def clean_token(value: Any, label: str = "identifier") -> str:
    """Lower-case identifier (adapter/core/emulator names). Empty is allowed; anything else must be a plain token,
    because these values end up in file names and emulator settings."""
    text = str(value or "").strip().lower()
    if text and not _TOKEN.fullmatch(text):
        raise MultiplayerError("invalid_identifier", f"{label} is not a valid name")
    return text


def clean_version(value: Any) -> str:
    return _VERSION_TEXT.sub("", clean_text(value, 64))


def clean_nickname(value: Any, fallback: str = "Drone") -> str:
    text = _NICKNAME_BAD.sub("", clean_text(value, 24)).strip()
    return text or fallback


def clean_mechanism(value: Any, allow_empty: bool = False) -> str:
    text = str(value or "").strip().upper()
    if not text and allow_empty:
        return ""
    if text not in MECHANISMS:
        raise MultiplayerError("invalid_mechanism", f"mechanism must be one of: {', '.join(MECHANISMS)}")
    return text


def clean_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise MultiplayerError("invalid_value", f"{name} must be an integer")
    if isinstance(value, str) and (len(value) > 20 or not re.fullmatch(r"[+-]?[0-9]+", value)):
        raise MultiplayerError("invalid_value", f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise MultiplayerError("invalid_value", f"{name} must be an integer") from None
    if not minimum <= number <= maximum:
        raise MultiplayerError("invalid_value", f"{name} must be between {minimum} and {maximum}")
    return number


def clean_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise MultiplayerError("invalid_value", f"{name} must be true or false")


def require_dict(value: Any, name: str = "request body") -> dict:
    if not isinstance(value, dict):
        raise MultiplayerError("invalid_body", f"{name} must be a JSON object")
    return value


def config_fingerprint(*parts: Any) -> str:
    """Stable identifier of one tested configuration (mechanism + emulator + versions)."""
    import hashlib
    joined = "\x1f".join(str(part or "").strip().lower() for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def normalize_title(name: str) -> str:
    """Canonical game identity for cross-peer matching.

    Drops the extension, bracketed/parenthesised tags (region, revision, dump
    info) and punctuation, so ``Super Mario Kart (USA).sfc`` and
    ``Super Mario Kart (Europe) [!].zip`` share a key. A tag-only name keeps
    its text rather than collapsing to the empty string.
    """
    base = str(name or "").rsplit("/", 1)[-1]
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", base)
    stripped = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", stem)
    key = re.sub(r"[^a-z0-9]+", " ", (stripped or stem).lower()).strip()
    return key or re.sub(r"[^a-z0-9]+", " ", stem.lower()).strip()
