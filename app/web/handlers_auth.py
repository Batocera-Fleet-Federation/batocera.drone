"""RomRequestHandler login/logout/session-status handlers, as a mixin.

Backs the SPA's login page (replacing the old native-browser Basic Auth
prompt): ``GET /auth/session`` (public, "am I logged in"), ``POST /auth/login``
(public, verifies credentials and starts a session), ``POST /auth/logout``
(public/no-op-safe, ends the caller's own session). All three are dispatched
*before* the session-cookie gate in ``api_routes.py`` -- a browser with no
cookie yet must be able to reach them. Composed onto ``RomRequestHandler``.
"""

try:
    from ..common.auth import build_session_cookie, clear_session_cookie, record_unauthorized_response
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.auth import build_session_cookie, clear_session_cookie, record_unauthorized_response  # type: ignore


class HandlersAuthMixin:
    def _handle_auth_session(self) -> None:
        session = self.auth.authenticate_request(
            self.headers, self.client_address[0] if self.client_address else None
        )
        if session is None:
            self._send_json(200, {"authenticated": False})
            return
        self._send_json(200, {"authenticated": True, "username": session["username"]})

    def _handle_auth_login(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        token = self.auth.login(username, password) if username and password else None
        if token is None:
            self._send_json(401, {"error": "invalid username or password"})
            client_ip = self.client_address[0] if self.client_address else "-"
            record_unauthorized_response(client_ip)
            return
        cookie = build_session_cookie(token, secure=not self.settings.http_only)
        self._send_json(200, {"status": "ok", "username": username}, extra_headers={"Set-Cookie": cookie})

    def _handle_auth_logout(self) -> None:
        session = self.auth.authenticate_request(
            self.headers, self.client_address[0] if self.client_address else None
        )
        if session is not None:
            self.auth.session_store.revoke(session["token"])
        cookie = clear_session_cookie(secure=not self.settings.http_only)
        self._send_json(200, {"status": "logged_out"}, extra_headers={"Set-Cookie": cookie})
