"""Model Context Protocol (MCP) server for the Drone.

Exposes the Drone as an MCP server so a user can wire their AI assistant
(Claude Code / Codex / ...) into it. Transport is **Streamable HTTP** in its
simplest, stateless form: the client POSTs one JSON-RPC 2.0 request to
``/v1/api/mcp`` and gets one JSON response back (no SSE stream, no session id).

Auth is a single user-generated bearer token (``common/mcp_auth.py``), separate
from the browser session cookie -- remote MCP clients send
``Authorization: Bearer <token>``.

Each tool (``web/mcp_tools.py``) is a thin proxy over an existing Drone REST
endpoint, called back over the loopback interface where the auth layer already
trusts on-device traffic. That keeps the tool surface in lockstep with the real
API and avoids a second copy of any logic.

Composed onto ``RomRequestHandler``. Stdlib only.
"""

from __future__ import annotations

import http.client
import json
import ssl
from typing import Any, Optional
from urllib.parse import urlencode

try:
    from ..app_version import drone_app_version as _drone_app_version
    from ..common import mcp_auth as _mcp_auth
    from ..common.auth import record_unauthorized_response as _record_unauthorized_response
    from .route_config import API_PREFIX
    from . import mcp_tools as _mcp_tools
except ImportError:  # pragma: no cover - direct script execution fallback
    from app_version import drone_app_version as _drone_app_version  # type: ignore
    from common.auth import record_unauthorized_response as _record_unauthorized_response  # type: ignore
    from common import mcp_auth as _mcp_auth  # type: ignore
    from web.route_config import API_PREFIX  # type: ignore
    from web import mcp_tools as _mcp_tools  # type: ignore


DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-06-18", "2025-03-26", "2024-11-05"}


class _LoopbackApiError(RuntimeError):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class _LoopbackContext:
    """Calls the Drone's own REST API over 127.0.0.1 (trusted as on-device)."""

    def __init__(self, handler):
        self._port = int(handler.server.server_address[1])
        self._tls = isinstance(getattr(handler, "request", None), ssl.SSLSocket)

    def _request(self, method: str, path: str, query: Optional[dict], body: Optional[dict]) -> Any:
        url = API_PREFIX + path
        if query:
            pairs = [(k, v) for k, v in query.items() if v is not None and v != ""]
            if pairs:
                url += "?" + urlencode(pairs)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._tls:
            conn = http.client.HTTPSConnection(
                "127.0.0.1", self._port, timeout=60, context=ssl._create_unverified_context()
            )
        else:
            conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=60)
        try:
            conn.request(method, url, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        finally:
            conn.close()
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = text
        if status >= 400:
            detail = parsed.get("error") if isinstance(parsed, dict) else parsed
            raise _LoopbackApiError(status, detail or text or "request failed")
        return parsed

    def get(self, path: str, **query) -> Any:
        return self._request("GET", path, query, None)

    def post(self, path: str, body: Optional[dict] = None) -> Any:
        return self._request("POST", path, None, body or {})


class McpServerMixin:
    # -- admin token management (browser/session gated) -------------------
    def _handle_admin_mcp_status(self) -> None:
        status = _mcp_auth.token_status(self.settings)
        self._send_json(200, {
            **status,
            "endpoint": f"{API_PREFIX}/mcp",
            "transport": "streamable-http",
            "tool_count": len(_mcp_tools.TOOLS),
        })

    def _handle_admin_mcp_token(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        created = _mcp_auth.generate_token(self.settings, payload.get("label"))
        self._send_json(200, {**created, "endpoint": f"{API_PREFIX}/mcp"})

    def _handle_admin_mcp_revoke(self) -> None:
        _mcp_auth.revoke_token(self.settings)
        self._send_json(200, {"configured": False})

    def _record_mcp_auth_failure(self) -> None:
        client_ip = self.client_address[0] if self.client_address else "-"
        try:
            _record_unauthorized_response(client_ip)
        except Exception:  # pragma: no cover - never let logging break the response
            pass

    # -- the MCP endpoint (bearer-token gated) ---------------------------
    def _handle_mcp_rpc(self) -> None:
        auth_header = self.headers.get("Authorization", "") if self.headers is not None else ""
        token = auth_header[7:].strip() if auth_header[:7].lower() == "bearer " else ""
        if not _mcp_auth.verify_token(self.settings, token):
            self._send_json(
                401,
                {"error": "invalid or missing MCP bearer token"},
                extra_headers={"WWW-Authenticate": 'Bearer realm="batocera-drone-mcp"'},
            )
            # Feed the same brute-force blocker every other 401 path uses.
            self._record_mcp_auth_failure()
            return

        try:
            message = self._read_json_body()
        except ValueError:
            self._send_json(200, _rpc_error(None, -32700, "Parse error"))
            return

        response = self._dispatch_mcp(message)
        if response is None:
            self._send_empty(202)
        else:
            self._send_json(200, response)

    def _dispatch_mcp(self, message: dict) -> Optional[dict]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Invalid Request")
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message

        try:
            if method == "initialize":
                result = _initialize_result(params)
            elif method in ("notifications/initialized", "notifications/cancelled"):
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": _mcp_tools.list_tools()}
            elif method == "tools/call":
                result = self._call_mcp_tool(params)
            elif method in ("resources/list", "prompts/list"):
                result = {method.split("/")[0]: []}
            else:
                if is_notification:
                    return None
                return _rpc_error(msg_id, -32601, f"Method not found: {method}")
        except _LoopbackApiError as error:
            return _rpc_error(msg_id, -32000, str(error.detail) or "Drone API error")
        except ValueError as error:
            return _rpc_error(msg_id, -32602, str(error))
        except Exception as error:  # pragma: no cover - defensive
            return _rpc_error(msg_id, -32603, f"Internal error: {error}")

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _call_mcp_tool(self, params: dict) -> dict:
        name = str((params or {}).get("name") or "").strip()
        arguments = (params or {}).get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        tool = _mcp_tools.get_tool(name)
        if tool is None:
            raise ValueError(f"unknown tool: {name}")
        ctx = _LoopbackContext(self)
        try:
            payload = tool["call"](ctx, arguments)
            return {
                "content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}],
                "structuredContent": payload if isinstance(payload, dict) else {"result": payload},
                "isError": False,
            }
        except _LoopbackApiError as error:
            return {
                "content": [{"type": "text", "text": f"Drone API error (HTTP {error.status}): {error.detail}"}],
                "isError": True,
            }
        except ValueError as error:
            return {"content": [{"type": "text", "text": f"Invalid arguments: {error}"}], "isError": True}


def _rpc_error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _initialize_result(params: dict) -> dict:
    requested = str((params or {}).get("protocolVersion") or "").strip()
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "batocera-drone", "version": _drone_app_version()},
        "instructions": (
            "Batocera Drone MCP server. Read-only tools cover assets, gamelists, BIOS, "
            "controls, swarm/tailnet/VPN, transfers, torrents, system info, logs, emulator "
            "configs, email and automation. A few tools write: screen mode, volume, music "
            "volume, screensaver, automation toggles, and artwork scraping."
        ),
    }
