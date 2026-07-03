"""Authentication and abuse-protection for the Drone HTTP surface.

Extracted from ``drone_api.py``. Holds:

* ``DroneCredentialStore`` — PBKDF2-hashed local credentials persisted in the
  shared state DB (with an env/default fallback), and ``BasicAuth`` which checks
  an ``Authorization: Basic`` header against it.
* the brute-force 401 blocker (``record_unauthorized_response`` / ``is_ip_blocked``)
  and the unauthenticated-request rate limiter (``_unauthenticated_request_allowed``),
  with their tuneable env constants and in-memory per-IP state.

Loopback/self traffic is always exempt so on-device tooling can't lock itself out.
``drone_api`` re-exports these names, and the request handler reads the constants
through that re-export, so existing call sites and tests keep working.
"""

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

try:
    from ..storage.state_store import database_path_for_legacy_file as _state_database_path_for_legacy_file
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path_for_legacy_file as _state_database_path_for_legacy_file  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore


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


class BasicAuth:
    def __init__(self, username: Optional[str], password: Optional[str], credential_store: Optional[DroneCredentialStore] = None):
        self.username = username
        self.password = password
        self.credential_store = credential_store

    def check(self, header_value: Optional[str]) -> bool:
        if not header_value or not header_value.startswith("Basic "):
            return False

        try:
            encoded = header_value.split(" ", 1)[1].strip()
            decoded = base64.b64decode(encoded).decode("utf-8")
            user, pw = decoded.split(":", 1)
            if self.credential_store:
                return self.credential_store.check(user, pw)
            if not self.username or not self.password:
                return True
            return user == self.username and pw == self.password
        except Exception:
            return False


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
