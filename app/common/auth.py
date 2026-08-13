"""Authentication and abuse-protection for the Drone HTTP surface.

Extracted from ``drone_api.py``. Holds:

* ``DroneCredentialStore`` — PBKDF2-hashed local credentials persisted in the
  shared state DB (with an env/default fallback).
* ``SessionStore`` + ``SessionAuth`` — cookie-based login, replacing the old
  ``BasicAuth``/``Authorization: Basic`` scheme. A browser's native Basic-auth
  prompt is invasive for humans and awkward for automation (every call needs
  the credential re-attached); a real login form plus a session cookie the
  browser carries automatically is friendlier for both. Sessions are rows in
  the shared state DB (``storage/state_store.py``'s ``sessions`` table, TTL
  ``DRONE_SESSION_TTL_SECONDS`` default 30 days, sliding: ``SessionStore.validate``
  extends ``expires_at`` on use, throttled to at most once per
  ``DRONE_SESSION_TOUCH_THROTTLE_SECONDS`` so an active session doesn't write
  the DB on every request). CSRF mitigation is the cookie's ``SameSite=Lax``
  attribute plus every mutating endpoint requiring a JSON body a cross-site
  HTML form cannot produce -- proportionate for a LAN-local admin tool, not a
  reason to add a separate CSRF-token scheme.
* the brute-force 401 blocker (``record_unauthorized_response`` / ``is_ip_blocked``)
  and the unauthenticated-request rate limiter (``_unauthenticated_request_allowed``),
  with their tuneable env constants and in-memory per-IP state. Failed logins
  feed the same blocker as failed session checks.

Loopback/self traffic is always exempt so on-device tooling can't lock itself out.
``drone_api`` re-exports these names, and the request handler reads the constants
through that re-export, so existing call sites and tests keep working.
"""

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from threading import Lock
from typing import Optional

try:
    from ..storage.state_store import database_path_for_legacy_file as _state_database_path_for_legacy_file
    from ..storage.state_store import create_session as _create_session
    from ..storage.state_store import delete_all_sessions as _delete_all_sessions
    from ..storage.state_store import delete_expired_sessions as _delete_expired_sessions
    from ..storage.state_store import delete_session as _delete_session
    from ..storage.state_store import get_session as _get_session
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..storage.state_store import touch_session as _touch_session
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path_for_legacy_file as _state_database_path_for_legacy_file  # type: ignore
    from storage.state_store import create_session as _create_session  # type: ignore
    from storage.state_store import delete_all_sessions as _delete_all_sessions  # type: ignore
    from storage.state_store import delete_expired_sessions as _delete_expired_sessions  # type: ignore
    from storage.state_store import delete_session as _delete_session  # type: ignore
    from storage.state_store import get_session as _get_session  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from storage.state_store import touch_session as _touch_session  # type: ignore


SESSION_COOKIE_NAME = os.environ.get("DRONE_SESSION_COOKIE_NAME", "drone_session")
DRONE_SESSION_TTL_SECONDS = max(60.0, float(os.environ.get("DRONE_SESSION_TTL_SECONDS", str(60 * 60 * 24 * 30))))
DRONE_SESSION_TOUCH_THROTTLE_SECONDS = max(0.0, float(os.environ.get("DRONE_SESSION_TOUCH_THROTTLE_SECONDS", str(6 * 60 * 60))))


DRONE_LOG_UNAUTHORIZED_REQUESTS = os.environ.get("DRONE_LOG_UNAUTHORIZED_REQUESTS", "0").strip().lower() in {"1", "true", "yes", "on"}
DRONE_UNAUTH_RATE_LIMIT_ENABLED = os.environ.get("DRONE_UNAUTH_RATE_LIMIT_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
DRONE_UNAUTH_RATE_LIMIT_REQUESTS = max(1, int(os.environ.get("DRONE_UNAUTH_RATE_LIMIT_REQUESTS", "60")))
DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS = max(1.0, float(os.environ.get("DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS", "60")))

# Brute-force protection: temporarily block an IP that produces too many 401
# (unauthorized) responses in a short window. Defaults: 5 failures / 60s -> 5 min block.
DRONE_AUTH_BLOCK_ENABLED = os.environ.get("DRONE_AUTH_BLOCK_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
DRONE_AUTH_BLOCK_THRESHOLD = max(1, int(os.environ.get("DRONE_AUTH_BLOCK_THRESHOLD", "5")))
DRONE_AUTH_BLOCK_WINDOW_SECONDS = max(1.0, float(os.environ.get("DRONE_AUTH_BLOCK_WINDOW_SECONDS", "60")))
DRONE_AUTH_BLOCK_DURATION_SECONDS = max(1.0, float(os.environ.get("DRONE_AUTH_BLOCK_DURATION_SECONDS", "300")))


class DroneCredentialStore:
    DEFAULT_USERNAME = "batocera"
    DEFAULT_PASSWORD = "linux"
    STATE_NAMESPACE = "credentials"

    def __init__(
        self,
        path: Path,
        env_username: Optional[str] = None,
        env_password: Optional[str] = None,
        state_database_file: Optional[Path] = None,
    ):
        self.path = path
        self.env_username = env_username
        self.env_password = env_password
        self.state_database_file = state_database_file or _state_database_path_for_legacy_file(path)
        self._lock = Lock()

    def _hash_password(self, password: str, salt: Optional[str] = None) -> str:
        salt_value = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_value.encode("ascii"), 240000)
        return f"pbkdf2_sha256$240000${salt_value}${digest.hex()}"

    def _verify_hash(self, password: str, stored: str) -> bool:
        try:
            scheme, rounds, salt, digest = stored.split("$", 3)
            if scheme != "pbkdf2_sha256":
                return False
            candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), int(rounds))
            return hmac.compare_digest(candidate.hex(), digest)
        except Exception:
            return False

    def load(self) -> dict:
        data = _load_state_payload(
            self.state_database_file,
            self.STATE_NAMESPACE,
            {},
            legacy_path=self.path,
        )
        if isinstance(data, dict) and data.get("username") and data.get("password_hash"):
            return data
        username = self.env_username or self.DEFAULT_USERNAME
        password = self.env_password or self.DEFAULT_PASSWORD
        return {"username": username, "password_plain_fallback": password, "source": "default"}

    def check(self, username: str, password: str) -> bool:
        data = self.load()
        if not hmac.compare_digest(username, str(data.get("username") or "")):
            return False
        password_hash = data.get("password_hash")
        if password_hash:
            return self._verify_hash(password, str(password_hash))
        return hmac.compare_digest(password, str(data.get("password_plain_fallback") or ""))

    def update(self, username: str, password: str) -> dict:
        username = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9._@-]{3,64}", username):
            raise ValueError("username must be 3-64 characters using letters, numbers, dot, dash, underscore, or @")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        with self._lock:
            data = {
                "username": username,
                "password_hash": self._hash_password(password),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_state_payload(
                self.state_database_file,
                self.STATE_NAMESPACE,
                data,
            )
            self.path.unlink(missing_ok=True)
            return {"username": username, "updated_at": data["updated_at"], "stored": True}


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_session_cookie(token: str, *, secure: bool, max_age: Optional[int] = None) -> str:
    """A ``Set-Cookie`` header value for a fresh (or refreshed) session.

    ``HttpOnly`` keeps it out of reach of any injected/XSS'd JS; ``SameSite=Lax``
    is the CSRF mitigation described in this module's docstring; ``Secure`` is
    added whenever the connection is TLS (the caller decides based on
    ``settings.http_only``) so the cookie is never sent in the clear.
    """
    parts = [f"{SESSION_COOKIE_NAME}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    return "; ".join(parts)


def clear_session_cookie(*, secure: bool) -> str:
    return build_session_cookie("", secure=secure, max_age=0)


class SessionStore:
    """Cookie-session persistence + sliding-expiry validation.

    Sessions are rows in the shared state DB (see ``storage/state_store.py``),
    so they survive a Drone restart -- important here since self-updates and
    the service-bootstrap restart path already happen fairly often; forcing a
    fresh login every time would be exactly the "invasive" friction this
    feature replaces.
    """

    def __init__(self, state_database_file: Path, *, ttl_seconds: float = DRONE_SESSION_TTL_SECONDS):
        self.state_database_file = state_database_file
        self.ttl_seconds = ttl_seconds

    def _expiry_from_now(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)).replace(microsecond=0).isoformat()

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        _create_session(self.state_database_file, token, username, self._expiry_from_now())
        try:  # opportunistic sweep so abandoned (never-logged-out) sessions don't accumulate forever
            _delete_expired_sessions(self.state_database_file, _iso_now())
        except Exception:
            pass
        return token

    def validate(self, token: Optional[str]) -> Optional[str]:
        """Return the session's username if ``token`` is live and unexpired, else None."""
        token = str(token or "").strip()
        if not token:
            return None
        session = _get_session(self.state_database_file, token)
        if session is None:
            return None
        if str(session["expires_at"]) <= _iso_now():
            _delete_session(self.state_database_file, token)
            return None
        try:
            last_seen = datetime.fromisoformat(str(session["last_seen_at"]))
            if (datetime.now(timezone.utc) - last_seen).total_seconds() >= DRONE_SESSION_TOUCH_THROTTLE_SECONDS:
                _touch_session(self.state_database_file, token, self._expiry_from_now())
        except Exception:
            pass
        return str(session["username"])

    def revoke(self, token: Optional[str]) -> None:
        if token:
            _delete_session(self.state_database_file, token)

    def revoke_all(self, *, except_token: Optional[str] = None) -> int:
        return _delete_all_sessions(self.state_database_file, except_token=except_token)


class SessionAuth:
    """Cookie-session gate for the admin/browser HTTP surface (replaces BasicAuth)."""

    def __init__(self, credential_store: DroneCredentialStore, session_store: SessionStore):
        self.credential_store = credential_store
        self.session_store = session_store

    def token_from_headers(self, headers) -> Optional[str]:
        raw_cookie = headers.get("Cookie") if headers is not None else None
        if not raw_cookie:
            return None
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(raw_cookie)
        except Exception:
            return None
        morsel = jar.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    def authenticate_request(self, headers, client_ip: Optional[str] = None) -> Optional[dict]:
        """Return ``{"token", "username"}`` if ``headers`` carries a live session cookie,
        or if ``client_ip`` is loopback.

        Physical/on-device access is already this codebase's root of trust (Drone
        runs as root; the top-level README says outright "only run it on machines
        and networks you trust"). On-device tooling that can only ever reach Drone
        via 127.0.0.1 -- the Ports client (``ports-client/``), which runs on the
        same device by design -- is implicitly pre-authenticated the same way a
        person already sitting at the console is, so it never needs its own login
        screen. Reuses ``_auth_block_exempt_ip``, the same loopback exemption the
        brute-force blocker already applies below, here at the actual auth gate
        instead of just its abuse protection.
        """
        token = self.token_from_headers(headers)
        if token is not None:
            username = self.session_store.validate(token)
            if username is not None:
                return {"token": token, "username": username}
        if client_ip and _auth_block_exempt_ip(client_ip):
            return {"token": None, "username": self.credential_store.load().get("username") or "local"}
        return None

    def login(self, username: str, password: str) -> Optional[str]:
        """Verify credentials against the credential store; on success, start and
        return a new session token."""
        if not self.credential_store.check(username, password):
            return None
        return self.session_store.create(username)


_UNAUTH_RATE_LIMIT_BUCKETS: "defaultdict[str, deque]" = defaultdict(deque)
_UNAUTH_RATE_LIMIT_LOCK = Lock()

# Brute-force auth blocker state. ``_AUTH_401_BUCKETS`` holds recent 401 timestamps
# per client IP (monotonic clock); ``_AUTH_BLOCKED_IPS`` maps a blocked IP to the
# monotonic time it should be unblocked. Both guarded by ``_AUTH_BLOCK_LOCK``.
_AUTH_401_BUCKETS: "defaultdict[str, deque]" = defaultdict(deque)
_AUTH_BLOCKED_IPS: "dict[str, float]" = {}
_AUTH_BLOCK_LOCK = Lock()


def _auth_block_exempt_ip(client_ip: str) -> bool:
    """Never block loopback so the local UI / on-device tooling can't lock itself out."""
    try:
        address = ipaddress.ip_address(str(client_ip or "").split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def record_unauthorized_response(client_ip: str, now: Optional[float] = None) -> bool:
    """Record a 401 for ``client_ip``; block the IP if it crosses the threshold.

    Returns True when this 401 triggered a new block. Blocking is logged to stdout
    (visible in the Drone container/service logs). Self-traffic (loopback) is exempt.
    """
    if not DRONE_AUTH_BLOCK_ENABLED:
        return False
    ip = str(client_ip or "-")
    if ip == "-" or _auth_block_exempt_ip(ip):
        return False
    timestamp = time.monotonic() if now is None else float(now)
    cutoff = timestamp - DRONE_AUTH_BLOCK_WINDOW_SECONDS
    with _AUTH_BLOCK_LOCK:
        if ip in _AUTH_BLOCKED_IPS:
            return False  # already blocked; nothing more to count
        bucket = _AUTH_401_BUCKETS[ip]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        bucket.append(timestamp)
        if len(bucket) < DRONE_AUTH_BLOCK_THRESHOLD:
            return False
        _AUTH_BLOCKED_IPS[ip] = timestamp + DRONE_AUTH_BLOCK_DURATION_SECONDS
        _AUTH_401_BUCKETS.pop(ip, None)
    print(
        f"Auth block: ip={ip} blocked after {DRONE_AUTH_BLOCK_THRESHOLD} unauthorized "
        f"requests within {int(DRONE_AUTH_BLOCK_WINDOW_SECONDS)}s; "
        f"blocked for {int(DRONE_AUTH_BLOCK_DURATION_SECONDS)}s",
        file=sys.stdout,
        flush=True,
    )
    return True


def is_ip_blocked(client_ip: str, now: Optional[float] = None) -> bool:
    """Return True if ``client_ip`` is currently blocked, expiring stale blocks lazily."""
    if not DRONE_AUTH_BLOCK_ENABLED:
        return False
    ip = str(client_ip or "-")
    if ip == "-" or _auth_block_exempt_ip(ip):
        return False
    timestamp = time.monotonic() if now is None else float(now)
    with _AUTH_BLOCK_LOCK:
        blocked_until = _AUTH_BLOCKED_IPS.get(ip)
        if blocked_until is None:
            return False
        if timestamp >= blocked_until:
            # 5-minute (configurable) block elapsed: unblock and start fresh.
            _AUTH_BLOCKED_IPS.pop(ip, None)
            _AUTH_401_BUCKETS.pop(ip, None)
            return False
        return True


def _is_external_client_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value or "").split("%", 1)[0])
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


def _unauthenticated_request_allowed(client_ip: str, now: Optional[float] = None) -> bool:
    if not DRONE_UNAUTH_RATE_LIMIT_ENABLED:
        return True
    if not _is_external_client_ip(client_ip):
        return True
    timestamp = time.monotonic() if now is None else float(now)
    cutoff = timestamp - DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS
    with _UNAUTH_RATE_LIMIT_LOCK:
        bucket = _UNAUTH_RATE_LIMIT_BUCKETS[str(client_ip or "-")]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= DRONE_UNAUTH_RATE_LIMIT_REQUESTS:
            return False
        bucket.append(timestamp)
        return True
