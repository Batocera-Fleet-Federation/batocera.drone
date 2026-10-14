"""Multiplayer HTTP handlers: the admin UI API and the peer-to-peer coordination API.

* ``/v1/api/admin/multiplayer/...`` -- the browser. Reads need the normal
  session gate; mutations need a real login (loopback pre-auth is not enough),
  ``admin_enabled``, a same-origin ``Origin`` and a JSON body, because they can
  launch games and edit emulator settings on this machine.
* ``/v1/api/peer/multiplayer/...`` -- mTLS only (``_peer_request_authorized``),
  reached on the peer listener. The caller's identity is the certificate's,
  never a body field. Peers can propose (invite/update/respond/launch/match);
  they can never start a game here without this machine's own confirmation.

No endpoint accepts a command line, a path, or a raw configuration key: inputs
are bounded identifiers and enumerated actions (see ``multiplayer.models``).
"""

from urllib.parse import urlparse

try:
    from ..multiplayer.models import MultiplayerError
    from ..multiplayer.orchestrator import get_orchestrator
except ImportError:  # pragma: no cover - flat execution
    from multiplayer.models import MultiplayerError  # type: ignore
    from multiplayer.orchestrator import get_orchestrator  # type: ignore


def _first(query_params: dict, name: str, default: str = "") -> str:
    values = query_params.get(name) or [default]
    return str(values[0] if values[0] is not None else default)


class HandlersMultiplayerMixin:
    def _multiplayer(self):
        return get_orchestrator(self.settings, self.repository)

    def _multiplayer_error(self, error: MultiplayerError) -> None:
        self._send_json(error.status, error.to_dict())

    def _multiplayer_actor(self) -> str:
        try:
            session = self.auth.authenticate_request(self.headers, self.client_address[0] if self.client_address else None)
        except Exception:  # noqa: BLE001 - attribution only
            session = None
        return str((session or {}).get("username") or "")

    def _multiplayer_origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return (parsed.scheme in ("http", "https") and not parsed.username and not parsed.password
                and bool(parsed.netloc) and parsed.netloc.lower() == str(self.headers.get("Host") or "").lower())

    # ----------------------------------------------------------------- admin GET
    def _handle_admin_multiplayer_get(self, parts: list, query_params: dict) -> None:
        if not self.settings.admin_enabled:
            self._send_json(403, {"error": "admin disabled"})
            return
        try:
            self._send_json(200, self._dispatch_multiplayer_get(self._multiplayer(), parts, query_params))
        except MultiplayerError as error:
            self._multiplayer_error(error)

    def _dispatch_multiplayer_get(self, mp, parts: list, query_params: dict):
        if not parts:
            return mp.overview()
        head, rest = parts[0], parts[1:]
        if head == "adapters":
            return {"adapters": mp.list_adapters()} if not rest else mp.adapter_detail(rest[0])
        if head == "peers" and not rest:
            return {"peers": mp.list_peers()}
        if head == "games":
            if rest == ["systems"]:
                return {"systems": mp.list_game_systems()}
            if rest == ["shared"]:
                return {"games": mp.shared_games()}
            if len(rest) == 2:
                return mp.game_detail(rest[0], rest[1])
            if not rest:
                return mp.list_games({name: _first(query_params, name) for name in query_params})
        if head == "sessions":
            if not rest:
                return {"sessions": mp.list_sessions(include_ended=_first(query_params, "ended") in ("1", "true"))}
            if len(rest) == 1:
                return mp.get_session(rest[0])
            if len(rest) == 2 and rest[1] == "diagnostics":
                return mp.diagnostics(rest[0])
        if head == "invitations" and not rest:
            return {"invitations": mp.list_invitations(include_closed=_first(query_params, "all") in ("1", "true"))}
        if head == "history" and not rest:
            return {"history": mp.history()}
        if head == "settings" and not rest:
            return mp.get_settings()
        if head == "compatibility" and not rest:
            return {"records": mp.list_compatibility()}
        raise MultiplayerError("not_found", "Unknown multiplayer endpoint.", 404)

    # ---------------------------------------------------------------- admin POST
    def _handle_admin_multiplayer_post(self, parts: list) -> None:
        if self.auth.authenticate_request(self.headers) is None:
            self._send_unauthorized()
            return
        if not self.settings.admin_enabled:
            self._send_json(403, {"error": "admin disabled"})
            return
        if not self._multiplayer_origin_ok():
            self._send_json(403, {"error": "cross-origin multiplayer requests are not allowed"})
            return
        content_type = str(self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._send_json(415, {"error": "multiplayer requests must send a JSON body (Content-Type: application/json)"})
            return
        try:
            body = self._read_json_body()
            self._send_json(*self._dispatch_multiplayer_post(self._multiplayer(), parts, body, self._multiplayer_actor()))
        except MultiplayerError as error:
            self._multiplayer_error(error)
        except ValueError as error:
            self._send_json(400, {"error": str(error), "code": "invalid_body"})

    def _dispatch_multiplayer_post(self, mp, parts: list, body: dict, actor: str):
        head, rest = (parts[0] if parts else ""), parts[1:]
        if head == "sessions":
            if not rest:
                return 201, mp.create_session(body, actor)
            if len(rest) == 2:
                session_id, action = rest
                if action == "invite":
                    return 200, mp.invite_peer(session_id, body.get("peer_id"), actor)
                if action == "assign-slot":
                    return 200, mp.assign_slot(session_id, body.get("peer_id"), body.get("slot"))
                if action == "remove-player":
                    return 200, mp.remove_player(session_id, body.get("peer_id"), actor)
                if action == "ready":
                    return 200, mp.set_ready(session_id, body.get("ready"))
                if action == "start":
                    return 202, mp.start_session(session_id, body.get("replace_running_game", False), actor)
                if action == "launch":
                    return 202, mp.confirm_launch(session_id, body.get("replace_running_game", False))
                if action == "retry":
                    return 202, mp.retry_connection(session_id, body.get("peer_id") or "")
                if action == "leave":
                    return 200, mp.leave_session(session_id, actor)
                if action == "end":
                    return 200, mp.end_session(session_id, actor)
                if action == "result":
                    return 200, mp.record_result(session_id, body.get("success"), str(body.get("note") or "")[:300])
                if action == "restore-config":
                    return 200, mp.restore_configuration(session_id)
        if head == "invitations" and len(rest) == 2:
            if rest[1] == "accept":
                return 200, mp.accept_invitation(rest[0], actor)
            if rest[1] == "decline":
                return 200, mp.decline_invitation(rest[0], actor)
        if head == "join" and not rest:
            return 200, mp.accept_invitation(body.get("session_id"), actor)
        if head == "settings" and not rest:
            return 200, mp.update_settings(body)
        if head == "adapters" and rest == ["refresh"]:
            return 200, {"adapters": mp.refresh_detection()}
        if head == "connectivity" and rest == ["test"]:
            return 200, mp.test_connectivity(body.get("peer_id"))
        if head == "compatibility" and rest == ["import"]:
            return 200, mp.import_compatibility_from_peer(body.get("peer_id"))
        raise MultiplayerError("not_found", "Unknown multiplayer endpoint.", 404)

    # ---------------------------------------------------------------------- peer
    def _handle_peer_multiplayer(self, parts: list, payload: dict, query_params: dict, method: str) -> None:
        """``/peer/multiplayer/<action>`` -- caller identity comes from the pinned client certificate."""
        if not self._peer_request_authorized():
            return
        caller = self._peer_requester_device_id()
        if not caller:
            self._send_json(403, {"error": "paired client identity could not be resolved"})
            return
        mp = self._multiplayer()
        action = parts[0] if parts else ""
        try:
            if method == "POST" and action == "invite":
                result = mp.peer_invite(caller, payload)
            elif method == "POST" and action == "update":
                result = mp.peer_update(caller, payload)
            elif method == "POST" and action == "respond":
                result = mp.peer_respond(caller, payload)
            elif method == "POST" and action == "launch":
                result = mp.peer_launch(caller, payload)
            elif method == "POST" and action == "match":
                result = mp.peer_match(caller, payload)
            elif method == "GET" and action == "status":
                result = mp.peer_status(caller, _first(query_params, "session"))
            elif method == "GET" and action == "compatibility":
                result = mp.peer_compat_export(caller)
            else:
                raise MultiplayerError("not_found", "Unknown multiplayer peer endpoint.", 404)
        except MultiplayerError as error:
            self._multiplayer_error(error)
            return
        self._send_json(200, result)
