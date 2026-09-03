import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path

from app.mock_data import seed_mock_userdata
from app.drone_api import Settings, create_server


class McpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name) / "userdata"
        seed_mock_userdata(self._root)

        self._old_env = dict(os.environ)
        os.environ.update({
            "USERDATA_ROOT": str(self._root),
            "ROMS_ROOT": str(self._root / "roms"),
            "BIOS_ROOT": str(self._root / "bios"),
            "SAVES_ROOT": str(self._root / "saves"),
            "DRONE_APP_USERNAME": "admin",
            "DRONE_APP_PASSWORD": "changeme",
            "HTTPS_PORT": "0",
            "HTTP_ONLY": "1",
            "LOG_DIR": str(Path(self._tmp.name) / "logs"),
            "ROM_METADATA_POLL_SECONDS": "0",
        })
        self.settings = Settings.from_env()
        try:
            self.server = create_server(self.settings)
        except PermissionError as error:
            self.skipTest(f"Socket bind is not allowed here: {error}")
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._cookie = self._login("admin", "changeme")
        self.token = self._generate_token()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        os.environ.clear()
        os.environ.update(self._old_env)
        self._tmp.cleanup()

    def _login(self, username: str, password: str) -> str:
        url = f"http://127.0.0.1:{self.port}/v1/api/auth/login"
        body = json.dumps({"username": username, "password": password}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            jar: SimpleCookie = SimpleCookie()
            jar.load(resp.headers.get("Set-Cookie"))
        morsel = next(iter(jar.values()))
        return f"{morsel.key}={morsel.value}"

    def _generate_token(self) -> str:
        url = f"http://127.0.0.1:{self.port}/v1/api/admin/mcp/token"
        req = urllib.request.Request(url, data=b"{}", method="POST")
        req.add_header("Cookie", self._cookie)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())["token"]

    def _rpc(self, method: str, params=None, *, token=None, msg_id=1):
        url = f"http://127.0.0.1:{self.port}/v1/api/mcp"
        payload = {"jsonrpc": "2.0", "method": method}
        if msg_id is not None:
            payload["id"] = msg_id
        if params is not None:
            payload["params"] = params
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        bearer = self.token if token is None else token
        if bearer:
            req.add_header("Authorization", f"Bearer {bearer}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    def test_initialize_handshake(self) -> None:
        status, body = self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["serverInfo"]["name"], "batocera-drone")
        self.assertIn("tools", body["result"]["capabilities"])

    def test_initialized_notification_returns_no_body(self) -> None:
        status, body = self._rpc("notifications/initialized", msg_id=None)
        self.assertEqual(status, 202)
        self.assertIsNone(body)

    def test_tools_list_covers_read_and_write(self) -> None:
        _, body = self._rpc("tools/list")
        names = {tool["name"] for tool in body["result"]["tools"]}
        for expected in ("list_asset_systems", "get_swarm", "get_vpn", "get_automation",
                         "set_screen_mode", "set_volume", "set_screensaver_minutes",
                         "scrape_asset_artwork"):
            self.assertIn(expected, names)
        for tool in body["result"]["tools"]:
            self.assertIn("inputSchema", tool)

    def test_tools_call_reads_asset_systems(self) -> None:
        _, body = self._rpc("tools/call", {"name": "list_asset_systems", "arguments": {}})
        result = body["result"]
        self.assertFalse(result.get("isError"))
        parsed = json.loads(result["content"][0]["text"])
        self.assertIn("systems", parsed)

    def test_tools_call_unknown_tool_is_invalid_params(self) -> None:
        _, body = self._rpc("tools/call", {"name": "no_such_tool", "arguments": {}})
        self.assertEqual(body["error"]["code"], -32602)

    def test_tools_call_surfaces_drone_api_error_as_error_result(self) -> None:
        _, body = self._rpc("tools/call", {"name": "set_volume", "arguments": {"level": 3}})
        self.assertTrue(body["result"]["isError"])

    def test_unknown_method_returns_jsonrpc_error(self) -> None:
        _, body = self._rpc("does/not/exist")
        self.assertEqual(body["error"]["code"], -32601)

    def test_missing_token_is_rejected(self) -> None:
        status, body = self._rpc("tools/list", token="")
        self.assertEqual(status, 401)

    def test_bad_token_is_rejected(self) -> None:
        status, _ = self._rpc("tools/list", token="dmcp_wrong")
        self.assertEqual(status, 401)

    def test_get_method_not_allowed(self) -> None:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/api/mcp")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 405")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 405)

    def test_revoke_disables_endpoint(self) -> None:
        url = f"http://127.0.0.1:{self.port}/v1/api/admin/mcp/revoke"
        req = urllib.request.Request(url, data=b"{}", method="POST")
        req.add_header("Cookie", self._cookie)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertFalse(json.loads(resp.read())["configured"])
        status, _ = self._rpc("tools/list")
        self.assertEqual(status, 401)

    def test_admin_status_reports_configured(self) -> None:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/api/admin/mcp")
        req.add_header("Cookie", self._cookie)
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        self.assertTrue(body["configured"])
        self.assertEqual(body["endpoint"], "/v1/api/mcp")
        self.assertGreater(body["tool_count"], 0)


if __name__ == "__main__":
    unittest.main()
