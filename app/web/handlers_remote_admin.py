"""Remote-administration proxy: manage a paired peer Drone from this Drone's UI.

Extracted as its own mixin (composed onto ``RomRequestHandler``, same pattern as
``HandlersPeerMixin``/``HandlersNetworkMixin``). Lets an already-authenticated
admin of *this* Drone select a paired peer and drive its entire existing
``/admin/*`` surface without leaving this Drone's UI -- reusing the peer's own
login (the same username/password used to sign into it directly) as the sole
authorization gate, so the target independently authenticates and authorizes
every action exactly as if the browser had connected to it directly. No new
role/permission model: whatever that login can do locally is exactly what it
can do remotely, nothing more.

Credentials for a peer are cached **only in this process's memory**, never on
disk and never returned to the browser -- entering them once (verified against
the peer's own ``/admin/system-info``) is enough for the rest of that session.
A service restart drops the cache, which is a deliberate, safer default: it
just means re-entering that peer's credentials next time.

Only lightweight admin JSON/text crosses this proxy. ROM/BIOS/save/artwork
*bytes* keep moving through the existing P2P transport (``app/transport/``)
directly between whichever two Drones are actually transferring -- this
feature never sits in that data path.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from threading import Lock
from typing import Optional
from urllib.parse import quote

try:
    from ..app_version import drone_app_version as _drone_app_version
    from ..transfer import local_network as _local_network
    from ..transfer.peer_connectivity import _peer_proxy_request
except ImportError:  # pragma: no cover - direct script execution fallback
    from app_version import drone_app_version as _drone_app_version  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.peer_connectivity import _peer_proxy_request  # type: ignore

# How long a verified peer session stays cached before requiring re-entry of
# that peer's credentials. Deliberately not persisted anywhere -- see module
# docstring.
REMOTE_SESSION_TTL_SECONDS = max(60.0, float(os.environ.get("DRONE_REMOTE_SESSION_TTL_SECONDS", "1800")))

# Local copy of the peer-request timeout (peer_connectivity keeps its own default);
# proxied admin actions can be slower than a health check but must not hang the
# browser indefinitely.
REMOTE_PROXY_TIMEOUT_SECONDS = float(os.environ.get("DRONE_REMOTE_PROXY_TIMEOUT_SECONDS", "15"))

_REMOTE_SESSIONS: "dict[str, dict]" = {}
_REMOTE_SESSIONS_LOCK = Lock()


def _basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _cache_remote_session(peer_id: str, authorization: str, drone_app_version_value: str) -> None:
    with _REMOTE_SESSIONS_LOCK:
        _REMOTE_SESSIONS[peer_id] = {
            "authorization": authorization,
            "drone_app_version": drone_app_version_value,
            "cached_at": time.monotonic(),
        }


def _get_remote_session(peer_id: str) -> Optional[dict]:
    with _REMOTE_SESSIONS_LOCK:
        session = _REMOTE_SESSIONS.get(peer_id)
        if session is None:
            return None
        if time.monotonic() - session["cached_at"] > REMOTE_SESSION_TTL_SECONDS:
            _REMOTE_SESSIONS.pop(peer_id, None)
            return None
        return session


def _clear_remote_session(peer_id: str) -> None:
    with _REMOTE_SESSIONS_LOCK:
        _REMOTE_SESSIONS.pop(peer_id, None)


class HandlersRemoteAdminMixin:
    def _handle_admin_remote_status(self, peer_id: str) -> None:
        """Whether a live remote-admin session is already cached for ``peer_id``.

        Lets a newly opened impersonation tab skip the credential prompt when
        another tab (or an earlier visit, within the session TTL) already
        connected to the same peer -- sessions are cached server-side, shared
        across every tab, never per-browser-tab state.
        """
        peer_id = str(peer_id or "").strip()
        peer = _local_network.get_paired_peer(self.settings, peer_id) if peer_id else None
        session = _get_remote_session(peer_id) if peer_id else None
        self._send_json(
            200,
            {
                "connected": bool(peer and session),
                "peer_id": peer_id,
                "name": str((peer or {}).get("name") or peer_id),
            },
        )

    def _handle_admin_remote_connect(self, payload: dict) -> None:
        peer_id = str(payload.get("peer_id") or "").strip()
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        if not peer_id or not username or not password:
            self._send_json(400, {"error": "peer_id, username, and password are required"})
            return
        peer = _local_network.get_paired_peer(self.settings, peer_id)
        if not peer:
            self._send_json(404, {"error": "not a paired Drone"})
            return
        authorization = _basic_auth_header(username, password)
        try:
            response = _peer_proxy_request(
                peer,
                "GET",
                "/v1/api/admin/system-info",
                self.settings,
                authorization=authorization,
                peer_id=peer_id,
                config={"network_mode": "local_network"},
                timeout=REMOTE_PROXY_TIMEOUT_SECONDS,
            )
        except Exception as error:
            self._send_json(502, {"error": f"{peer.get('name') or peer_id} is offline or unreachable: {error}"})
            return
        if response.status == 401:
            self._send_json(401, {"error": f"invalid credentials for {peer.get('name') or peer_id}"})
            return
        if response.status == 403:
            self._send_json(409, {"error": f"admin is disabled on {peer.get('name') or peer_id}"})
            return
        if response.status != 200:
            self._send_json(502, {"error": f"could not connect to {peer.get('name') or peer_id} (status {response.status})"})
            return
        remote_version = ""
        try:
            remote_version = str((json.loads(response.body or b"{}").get("fields") or {}).get("drone_app_version") or "")
        except Exception:
            remote_version = ""
        _cache_remote_session(peer_id, authorization, remote_version)
        print(
            f"Remote-admin session started for peer {peer_id} ({peer.get('name') or 'unnamed'})",
            file=sys.stdout,
            flush=True,
        )
        self._send_json(
            200,
            {
                "status": "connected",
                "peer_id": peer_id,
                "name": str(peer.get("name") or peer_id),
                "drone_app_version": remote_version,
            },
        )

    def _handle_admin_remote_disconnect(self, payload: dict) -> None:
        peer_id = str(payload.get("peer_id") or "").strip()
        if peer_id:
            _clear_remote_session(peer_id)
        self._send_json(200, {"status": "disconnected", "peer_id": peer_id})

    def _handle_admin_remote_proxy(self, peer_id: str, sub_path: str, method: str, query_string: str) -> None:
        peer_id = str(peer_id or "").strip()
        peer = _local_network.get_paired_peer(self.settings, peer_id)
        if not peer:
            self._send_json(404, {"error": "not a paired Drone"})
            return
        session = _get_remote_session(peer_id)
        if session is None:
            self._send_json(401, {"error": "not_connected", "message": f"reconnect to {peer.get('name') or peer_id}"})
            return
        body: Optional[bytes] = None
        content_type: Optional[str] = None
        if method.upper() == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length < 0 or length > (256 * 1024):
                self._send_json(400, {"error": "request body too large"})
                return
            body = self.rfile.read(length) if length > 0 else b"{}"
            content_type = self.headers.get("Content-Type") or "application/json"
        target_path = "/v1/api/admin/" + quote(sub_path.lstrip("/"), safe="/")
        if query_string:
            target_path = f"{target_path}?{query_string}"
        peer_name = str(peer.get("name") or peer_id)
        try:
            response = _peer_proxy_request(
                peer,
                method,
                target_path,
                self.settings,
                body=body,
                authorization=session["authorization"],
                content_type=content_type,
                peer_id=peer_id,
                config={"network_mode": "local_network"},
                timeout=REMOTE_PROXY_TIMEOUT_SECONDS,
            )
        except Exception as error:
            print(f"Remote-admin proxy to {peer_id} failed: {method} {sub_path}: {error}", file=sys.stdout, flush=True)
            self._send_json(502, {"error": f"{peer_name} is offline or unreachable: {error}"})
            return
        if response.status == 401:
            _clear_remote_session(peer_id)
            print(f"Remote-admin proxy to {peer_id} rejected (credentials revoked/changed)", file=sys.stdout, flush=True)
            self._send_json(401, {"error": f"credentials for {peer_name} were rejected; reconnect"})
            return
        if response.status == 404:
            local_version = _drone_app_version()
            remote_version = str(session.get("drone_app_version") or "")
            detail = f"{peer_name} does not support this action"
            if remote_version and remote_version != local_version:
                detail += f" (this Drone is {local_version}, {peer_name} is {remote_version}) -- possible version mismatch"
            else:
                detail += " -- possible version mismatch"
            print(f"Remote-admin proxy to {peer_id} 404: {method} {sub_path}", file=sys.stdout, flush=True)
            self._send_json(404, {"error": detail})
            return
        print(
            f"Remote-admin proxy to {peer_id} ({peer_name}): {method} {sub_path} -> {response.status}",
            file=sys.stdout,
            flush=True,
        )
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self._send_security_headers()
        self.end_headers()
        if method.upper() != "HEAD":
            self.wfile.write(response.body)
