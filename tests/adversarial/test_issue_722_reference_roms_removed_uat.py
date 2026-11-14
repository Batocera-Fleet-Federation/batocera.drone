"""Issue #722: network-share / NFS referencing is removed from the API surface.

Quoted requirements:
- "Remove \"Reference ROMs\" (network-share / NFS peer-ROM referencing) from the Ports client and from the web UI and backend."
- "A peer that's still on an older version and calls the removed `/peer/nfs/*` endpoints gets a clean 404/410, not a 500."
- Remove `app/device/network_share_manager.py`, `app/device/nfs_export_manager.py`,
  `app/common/network_references.py`, `app/web/handlers_network_share.py`, the NFS
  authorize/revoke peer endpoints, routes, OpenAPI, MCP tools, drone.js UI, and drone.css.
- "the entries in `transfer/swarm_reset.py` (`network_share_manager.json`, `nfs_export_manager.json`, `revoke_exports_for_identity_change`)."
"""

from __future__ import annotations

import ast
import importlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


class _RemovedRouteHandler:
    """Enough of the live route mixin to dispatch one request."""

    def __init__(self, method: str, path: str, body: bytes) -> None:
        # Issue #722 deletes app/web/handlers_network_share.py. Importing that
        # mixin made these route checks crash before they could assert 404/410,
        # and contradicted test_reference_modules_and_reset_entries_are_removed.
        from app.web.api_routes import ApiRoutesMixin
        from app.web.handlers_peer import HandlersPeerMixin

        self.__class__ = type(
            "Handler",
            (HandlersPeerMixin, ApiRoutesMixin, _RemovedRouteHandler),
            {},
        )
        self.path = path
        self.command = method
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.client_address = ("10.1.2.3", 40000)
        self.server = SimpleNamespace(all_tls_servers=(), is_peer_mtls_listener=False)
        self.responses: list[tuple[int, dict]] = []
        self._read_json_body = _read_json_body.__get__(self, self.__class__)

    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.responses.append((int(status_code), payload if isinstance(payload, dict) else {"error": str(payload)}))

    def _reject_if_ip_blocked(self) -> bool:
        return False

    def _rate_limit_unauthenticated_external_request(self) -> bool:
        return False

    def _peer_request_authorized(self) -> bool:
        return True

    def _peer_requester_device_id(self) -> str:
        return "older-peer"

    def log_error(self, *_args) -> None:
        return None


def _read_json_body(self) -> dict:
    length = int(self.headers.get("Content-Length", "0") or "0")
    raw = self.rfile.read(length) if length else b""
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON body") from error
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def _dispatch(method: str, path: str, body: bytes = b"{}") -> tuple[int, dict]:
    handler = _RemovedRouteHandler(method, path, body)
    handler.auth = SimpleNamespace(authenticate_request=lambda *_args, **_kwargs: {"token": "session"})
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / "state.sqlite3"
        handler.settings = SimpleNamespace(
            admin_enabled=True,
            userdata_root=Path(tmp),
            state_database_file=database,
            device_id="issue-722-removed-routes",
        )
        with mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": str(database)}), mock.patch(
            "app.web.api_routes._identity_swarm_networking_allowed", return_value=True
        ):
            if method == "GET":
                handler.do_GET()
            else:
                handler.do_POST()
    if not handler.responses:
        raise AssertionError(f"{method} {path} produced no response")
    return handler.responses[-1]


class ReferenceRomsRemovalUat(unittest.TestCase):
    def test_old_peer_nfs_calls_are_404_or_410_not_500(self) -> None:
        # "A peer that's still on an older version and calls the removed `/peer/nfs/*` endpoints gets a clean 404/410, not a 500."
        cases = (
            ("POST", "/v1/api/peer/network-share/nfs/authorize", b'{"peer_id": "older-peer"}'),
            ("POST", "/v1/api/peer/network-share/nfs/revoke", b"{}"),
            ("POST", "/v1/api/peer/network-share/nfs/authorize", b""),
            ("POST", "/v1/api/peer/network-share/nfs/authorize", b"not-json"),
            ("POST", "/v1/api/peer/network-share/nfs/revoke", b"[]"),
            ("POST", "/v1/api/peer/network-share/nfs/authorize", b'{"unexpected": 1, "peer_id": ""}'),
            ("GET", "/v1/api/peer/network-share/nfs/authorize", b""),
        )
        for method, path, body in cases:
            status, payload = _dispatch(method, path, body)
            self.assertIn(status, (404, 410), f"{method} {path} body={body!r} -> {status} {payload}")
            self.assertNotEqual(status, 500)

    def test_admin_network_share_routes_are_gone(self) -> None:
        # "app/web/handlers_network_share.py ... routes in `api_routes.py`"
        cases = (
            ("GET", "/v1/api/admin/network-shares", b""),
            ("POST", "/v1/api/admin/network-shares/peer-old/enable", b"{}"),
            ("POST", "/v1/api/admin/network-shares/peer-old/disable", b"{}"),
            ("GET", "/v1/api/admin/network-reference", b""),
            ("POST", "/v1/api/admin/network-reference/selection", b'{"systems": ["gb"]}'),
        )
        for method, path, body in cases:
            status, payload = _dispatch(method, path, body)
            self.assertIn(status, (404, 410), f"{method} {path} -> {status} {payload}")
            self.assertNotEqual(status, 500)

    def test_openapi_and_mcp_no_longer_advertise_network_shares(self) -> None:
        # "Network-share/NFS referencing is removed from the web UI, API, OpenAPI spec, MCP tools"
        from app.web.mcp_tools import list_tools
        from app.web.openapi_spec import build_openapi_spec

        paths = build_openapi_spec("issue-722").get("paths") or {}
        for path in paths:
            self.assertNotIn("network-share", path)
            self.assertNotIn("network-reference", path)
        names = [tool["name"] for tool in list_tools()]
        self.assertNotIn("get_network_shares", names)
        for tool in list_tools():
            description = f"{tool.get('name', '')} {tool.get('description', '')}"
            self.assertNotIn("network-shares", description)
            self.assertNotIn("network share", description.lower())

    def test_web_ui_cannot_reenable_reference_roms(self) -> None:
        # "the related UI in `drone.js` and styles in `drone.css`"
        script = (ROOT / "app/web/static/js/drone.js").read_text(encoding="utf-8", errors="replace")
        style = (ROOT / "app/web/static/css/drone.css").read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("REFERENCE_ROMS_UI_ENABLED", script)
        self.assertNotIn("/admin/network-shares", script)
        self.assertNotIn("/peer/network-share/nfs", script)
        self.assertNotIn(".reference-roms-table", style)

    def test_reference_modules_and_reset_entries_are_removed(self) -> None:
        # "app/device/network_share_manager.py, app/device/nfs_export_manager.py, app/common/network_references.py"
        # "network-share handling in `roms/rom_systems.py`"
        # "the entries in `transfer/swarm_reset.py` (`network_share_manager.json`, `nfs_export_manager.json`, `revoke_exports_for_identity_change`)."
        for module_name in (
            "app.device.network_share_manager",
            "app.device.nfs_export_manager",
            "app.common.network_references",
            "app.web.handlers_network_share",
        ):
            with self.assertRaises(ImportError):
                importlib.import_module(module_name)

        rom_systems = ast.parse((ROOT / "app/roms/rom_systems.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(rom_systems):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        self.assertFalse(any("network_references" in name for name in imported))

        from app.transfer import swarm_reset

        self.assertNotIn("network_share_manager.json", swarm_reset.IDENTITY_BOUND_NAMESPACES)
        self.assertNotIn("nfs_export_manager.json", swarm_reset.IDENTITY_BOUND_NAMESPACES)
        source = ast.get_source_segment(
            (ROOT / "app/transfer/swarm_reset.py").read_text(encoding="utf-8"),
            next(node for node in ast.parse((ROOT / "app/transfer/swarm_reset.py").read_text(encoding="utf-8")).body if isinstance(node, ast.FunctionDef) and node.name == "reset_swarm_identity_state"),
        ) or ""
        self.assertNotIn("revoke_exports_for_identity_change", source)
        self.assertNotIn("nfs_export_manager", source)

    def test_poller_docs_drop_the_network_share_watchdog(self) -> None:
        # "the network-share watchdog poller (`drone_api.py` / `runtime_state.py`)"
        # "docs/skills: `CLAUDE.md` (poller list), ... `docs/drone-identity.md`."
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        runtime = (ROOT / "app/common/runtime_state.py").read_text(encoding="utf-8")
        identity = (ROOT / "docs/drone-identity.md").read_text(encoding="utf-8")
        self.assertNotIn("network-share watchdog", claude)
        self.assertNotIn("network-share watchdog", runtime)
        self.assertNotIn("NFS-export authorizations/references", identity)
        self.assertNotIn("Live NFS client exports are revoked", identity)


if __name__ == "__main__":
    unittest.main()
