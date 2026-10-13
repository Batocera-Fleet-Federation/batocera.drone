"""Admin -> Drone identity HTTP handlers (``/admin/identity/...``).

Status is readable by any admin session. The three mutating actions -- pre-authorize a
hardware change, cancel that authorization, and resolve a pending recovery (keep the
existing identity on this hardware, or provision a new one) -- can preserve or replace the
swarm identity, so they require a real login session (loopback pre-authentication is not
enough), a same-origin JSON POST, and the administrator's password again. They are local to
this machine and never proxied or reachable through ``/peer/*``.
"""

import threading
import time
from collections import deque
from urllib.parse import urlparse

try:
    from ..common import identity_provisioning as _identity
    from ..common.auth import record_unauthorized_response
    from ..common.self_update import _restart_drone_process_soon
except ImportError:  # pragma: no cover - flat execution
    from common import identity_provisioning as _identity  # type: ignore
    from common.auth import record_unauthorized_response  # type: ignore
    from common.self_update import _restart_drone_process_soon  # type: ignore

_MAX_PASSWORD_CHARS = 1024
# Password re-entry guards actions that can swap the swarm identity. Loopback is exempt from the
# global IP blocker (on-device tooling), so identity actions carry their own failure throttle.
_MAX_FAILED_PASSWORDS = 5
_FAILURE_WINDOW_SECONDS = 300.0
_FAILED_ATTEMPTS: "deque[float]" = deque()
_FAILED_ATTEMPTS_LOCK = threading.Lock()


def _password_attempts_exhausted() -> bool:
    cutoff = time.monotonic() - _FAILURE_WINDOW_SECONDS
    with _FAILED_ATTEMPTS_LOCK:
        while _FAILED_ATTEMPTS and _FAILED_ATTEMPTS[0] < cutoff:
            _FAILED_ATTEMPTS.popleft()
        return len(_FAILED_ATTEMPTS) >= _MAX_FAILED_PASSWORDS


def _record_failed_password() -> None:
    with _FAILED_ATTEMPTS_LOCK:
        _FAILED_ATTEMPTS.append(time.monotonic())


def reset_failed_passwords_for_tests() -> None:
    with _FAILED_ATTEMPTS_LOCK:
        _FAILED_ATTEMPTS.clear()


class HandlersIdentityMixin:
    def _identity_session(self):
        session = self.auth.authenticate_request(self.headers)
        if session is None or not session.get("token"):
            self._send_unauthorized()
            return None
        if not self.settings.admin_enabled:
            self._send_json(403, {"error": "admin disabled"})
            return None
        return session

    def _identity_same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return (
            parsed.scheme in ("http", "https")
            and not parsed.username
            and not parsed.password
            and bool(parsed.netloc)
            and parsed.netloc.lower() == str(self.headers.get("Host") or "").lower()
        )

    def _identity_password_ok(self, session: dict, payload: dict) -> bool:
        password = payload.get("password")
        client_ip = self.client_address[0] if self.client_address else "-"
        if _password_attempts_exhausted():
            self._send_json(
                429,
                {"error": "too many incorrect passwords; try again in a few minutes"},
                extra_headers={"Retry-After": "300"},
            )
            return False
        if (
            not isinstance(password, str)
            or not password
            or len(password) > _MAX_PASSWORD_CHARS
            or not self.auth.credential_store.check(str(session.get("username") or ""), password)
        ):
            _record_failed_password()
            record_unauthorized_response(client_ip)
            self._send_json(403, {"error": "administrator password is required to change Drone identity"})
            return False
        return True

    def _handle_admin_identity_get(self, parts: list) -> None:
        if self._identity_session() is None:
            return
        if parts != ["status"]:
            self._send_json(404, {"error": "not found"})
            return
        self._send_json(200, _identity.status_payload(self.settings))

    def _handle_admin_identity_post(self, parts: list) -> None:
        session = self._identity_session()
        if session is None:
            return
        if not self._identity_same_origin():
            self._send_json(403, {"error": "cross-origin identity requests are not allowed"})
            return
        content_type = str(self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._send_json(415, {"error": "identity requests must send a JSON body (Content-Type: application/json)"})
            return
        payload = self._read_json_body()
        action = parts[0] if len(parts) == 1 else ""
        actor = str(session.get("username") or "")
        if action == "cancel-hardware-change":
            self._send_json(200, {"cancelled": _identity.cancel_hardware_change(self.settings)})
            return
        if action not in {"authorize-hardware-change", "resolve"}:
            self._send_json(404, {"error": "not found"})
            return
        if not self._identity_password_ok(session, payload):
            return
        try:
            if action == "authorize-hardware-change":
                authorization = _identity.authorize_hardware_change(self.settings, payload.get("hours"), actor=actor)
                self._send_json(200, {"authorized": True, "authorization": authorization})
                return
            resolved = _identity.resolve_recovery(self.settings, str(payload.get("action") or ""), actor=actor)
        except ValueError as error:
            self._send_json(409, {"error": str(error)})
            return
        except _identity.IdentityError as error:
            self._send_json(500, {"error": str(error)})
            return
        # Every subsystem must start under the resolved identity.
        _restart_drone_process_soon()
        self._send_json(200, {**resolved, "restarting": True})
