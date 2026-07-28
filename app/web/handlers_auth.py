"""RomRequestHandler login/logout/session-status handlers, as a mixin.

Backs the SPA's login/setup page (replacing the old native-browser Basic Auth
prompt): ``GET /auth/session`` (public, "am I logged in / is setup required"),
``POST /auth/setup`` (public only until initialization completes),
``POST /auth/login`` (public, verifies credentials and starts a session), and
``POST /auth/logout`` (public/no-op-safe, ends the caller's own session). They are dispatched
*before* the session-cookie gate in ``api_routes.py`` -- a browser with no
cookie yet must be able to reach them. Composed onto ``RomRequestHandler``.
"""

try:
    from ..common.auth import build_session_cookie, clear_session_cookie, record_unauthorized_response
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.auth import build_session_cookie, clear_session_cookie, record_unauthorized_response  # type: ignore


class HandlersAuthMixin:
    def _handle_auth_session(self) -> None:
        if not self.auth.credential_store.is_configured():
            self.auth.credential_store.ensure_setup_token()
            self._send_json(200, {"authenticated": False, "setup_required": True})
            return
        session = self.auth.authenticate_request(self.headers)
        if session is None:
            self._send_json(200, {"authenticated": False, "setup_required": False})
            return
        self._send_json(200, {"authenticated": True, "setup_required": False, "username": session["username"]})

    def _handle_auth_setup(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        password_confirmation = str(payload.get("password_confirmation") or "")
        setup_token = str(payload.get("setup_token") or "").strip()
        if password != password_confirmation:
            self._send_json(400, {"error": "password confirmation does not match"})
            return
        try:
            result = self.auth.credential_store.initialize(username, password, setup_token)
        except PermissionError:
            client_ip = self.client_address[0] if self.client_address else "-"
            record_unauthorized_response(client_ip)
            self._send_json(403, {"error": "invalid first-boot setup code"})
            return
        except RuntimeError as error:
            self._send_json(409, {"error": str(error)})
            return
        token = self.auth.session_store.create(result["username"])
        cookie = build_session_cookie(token, secure=not self.settings.http_only)
        self._send_json(
            201,
            {"status": "configured", "username": result["username"]},
            extra_headers={"Set-Cookie": cookie},
        )

    def _handle_auth_login(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        if not self.auth.credential_store.is_configured():
            self._send_json(409, {"error": "first-boot setup is required", "setup_required": True})
            return
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
        session = self.auth.authenticate_request(self.headers)
        if session is not None:
            self.auth.session_store.revoke(session["token"])
        cookie = clear_session_cookie(secure=not self.settings.http_only)
        self._send_json(200, {"status": "logged_out"}, extra_headers={"Set-Cookie": cookie})
