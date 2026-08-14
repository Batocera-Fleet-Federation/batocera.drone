"""Unit tests for DroneApiClient against a tiny fake Drone-shaped HTTP server.

Covers login/session/logout, error mapping, and the cookie-persistence
behavior that lets a relaunch of the app skip login (mirroring Drone's
30-day sliding session TTL) -- all without booting the real Drone server.
See test_http_client_integration.py for a test against the real thing.
"""

import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from client.config import ClientConfig
from client.errors import AuthenticationError, DroneApiError
from client.http_client import DroneApiClient

VALID_USERNAME = "batocera"
VALID_PASSWORD = "linux"
VALID_TOKEN = "test-session-token"


class _FakeDroneHandler(BaseHTTPRequestHandler):
    # Set by tests that need to inspect exactly what post_multipart() put
    # on the wire -- a class attribute so the test can read it after the
    # request completes, since a new handler instance is built per request.
    captured_multipart = None

    def log_message(self, format, *args):  # noqa: A002 - silence test server logging
        pass

    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if "=" in part and part.strip().startswith("drone_session"):
                return part.strip().split("=", 1)[1]
        return None

    def _send_json(self, status: int, payload: dict, set_cookie: str = None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        content_type = self.headers.get("Content-Type", "")
        authenticated = self._cookie_token() == VALID_TOKEN

        if self.path == "/v1/api/admin/vpn/upload" and authenticated:
            # Not JSON -- must not go through json.loads below.
            _FakeDroneHandler.captured_multipart = (content_type, raw)
            self._send_json(200, {"status": "ok", "config_filename": "captured.ovpn", "remotes": []})
            return

        payload = json.loads(raw or b"{}")

        if self.path == "/v1/api/auth/login":
            if payload.get("username") == VALID_USERNAME and payload.get("password") == VALID_PASSWORD:
                cookie = f"drone_session={VALID_TOKEN}; Path=/; HttpOnly; SameSite=Lax"
                self._send_json(200, {"status": "ok", "username": VALID_USERNAME}, set_cookie=cookie)
            else:
                self._send_json(401, {"error": "invalid username or password"})
            return

        if self.path == "/v1/api/auth/logout":
            cookie = "drone_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            self._send_json(200, {"status": "logged_out"}, set_cookie=cookie)
            return

        if self.path == "/v1/api/admin/vpn/connect" and authenticated:
            self._send_json(400, {"status": "error", "errors": ["no config uploaded", "no credentials saved"]})
            return

        self._send_json(404, {"error": "not found"})

    def do_GET(self):
        token = self._cookie_token()
        authenticated = token == VALID_TOKEN

        if self.path == "/v1/api/auth/session":
            if authenticated:
                self._send_json(200, {"authenticated": True, "username": VALID_USERNAME})
            else:
                self._send_json(200, {"authenticated": False})
            return

        if self.path.startswith("/v1/api/admin/system-info"):
            if not authenticated:
                self._send_json(401, {"error": "not authenticated"})
                return
            self._send_json(200, {"entries": [{"key": "Machine ID", "value": "test-device"}]})
            return

        self._send_json(404, {"error": "not found"})


class DroneApiClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), _FakeDroneHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.session_file = Path(self._tmp.name) / "session.json"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self._tmp.cleanup()

    def _make_config(self) -> ClientConfig:
        return ClientConfig(
            host="127.0.0.1",
            https_port=self.port,
            http_only=True,
            ca_cert_file=Path("/unused"),
            session_cookie_path=self.session_file,
        )

    def test_login_success_sets_cookie_and_session_status(self) -> None:
        client = DroneApiClient(self._make_config())
        username = client.login(VALID_USERNAME, VALID_PASSWORD)
        self.assertEqual(username, VALID_USERNAME)
        self.assertTrue(client.has_session_cookie)
        self.assertEqual(client.session_status(), {"authenticated": True, "username": VALID_USERNAME})

    def test_login_failure_raises_authentication_error(self) -> None:
        client = DroneApiClient(self._make_config())
        with self.assertRaises(AuthenticationError):
            client.login(VALID_USERNAME, "wrong-password")
        self.assertFalse(client.has_session_cookie)

    def test_unauthenticated_request_raises_authentication_error(self) -> None:
        client = DroneApiClient(self._make_config())
        with self.assertRaises(AuthenticationError):
            client.get("/admin/system-info")

    def test_session_persists_across_client_instances(self) -> None:
        first = DroneApiClient(self._make_config())
        first.login(VALID_USERNAME, VALID_PASSWORD)

        second = DroneApiClient(self._make_config())
        self.assertTrue(second.has_session_cookie)
        result = second.get("/admin/system-info")
        self.assertEqual(result, {"entries": [{"key": "Machine ID", "value": "test-device"}]})

    def test_logout_clears_persisted_session(self) -> None:
        client = DroneApiClient(self._make_config())
        client.login(VALID_USERNAME, VALID_PASSWORD)
        client.logout()

        reloaded = DroneApiClient(self._make_config())
        self.assertFalse(reloaded.has_session_cookie)

    def test_errors_array_response_joins_into_message(self) -> None:
        # Some endpoints (VPN connect/disconnect) return a plural "errors"
        # array instead of a singular "error" string -- drone.js itself
        # shipped with a bug where only "error" was read, silently discarding
        # the real reason. Confirm this client's error extraction handles both.
        client = DroneApiClient(self._make_config())
        client.login(VALID_USERNAME, VALID_PASSWORD)
        with self.assertRaises(DroneApiError) as ctx:
            client.post("/admin/vpn/connect")
        self.assertIn("no config uploaded", str(ctx.exception))
        self.assertIn("no credentials saved", str(ctx.exception))

    def test_post_multipart_produces_a_body_the_real_server_parser_accepts(self) -> None:
        # Captures the raw bytes DroneApiClient actually put on the wire,
        # then parses them with app/common/multipart.py's real parser (not
        # a hand-rolled fake one) -- proves wire-format compatibility with
        # the actual server, not just this test file's own assumptions.
        # conftest.py puts the repo root on sys.path so `app` is importable.
        from app.common.multipart import boundary_from_content_type, parse_multipart_files

        client = DroneApiClient(self._make_config())
        client.login(VALID_USERNAME, VALID_PASSWORD)

        # Content deliberately includes a CRLF sequence and non-ASCII bytes
        # -- a naive newline-normalizing bug would corrupt this silently.
        content = b"remote example.com 1194\r\nauth-user-pass\r\n\x00\xffbinary junk\r\n"
        result = client.post_multipart("/admin/vpn/upload", "config", "provider.ovpn", content)
        self.assertEqual(result["config_filename"], "captured.ovpn")

        content_type, raw_body = _FakeDroneHandler.captured_multipart
        boundary = boundary_from_content_type(content_type)
        files = parse_multipart_files(raw_body, boundary)
        self.assertEqual(files, [("provider.ovpn", content)])

    def test_unreachable_server_raises_drone_api_error(self) -> None:
        config = ClientConfig(
            host="127.0.0.1",
            https_port=1,  # nothing listens on port 1
            http_only=True,
            ca_cert_file=Path("/unused"),
            session_cookie_path=self.session_file,
        )
        client = DroneApiClient(config)
        with self.assertRaises(DroneApiError):
            client.get("/admin/system-info")

    def test_every_request_logs_a_start_and_success_line(self) -> None:
        # This is the actual debugging value requested after a live timeout
        # was hard to diagnose: every request's path, timeout budget, and
        # elapsed time must be visible from the log file alone.
        client = DroneApiClient(self._make_config())
        client.login(VALID_USERNAME, VALID_PASSWORD)
        out = io.StringIO()
        with redirect_stdout(out):
            client.get("/admin/system-info")
        lines = out.getvalue()
        self.assertRegex(lines, r"-> GET /admin/system-info timeout=15s")
        self.assertRegex(lines, r"<- GET /admin/system-info 200 in \d+\.\d+s")

    def test_unreachable_server_logs_the_failure_with_elapsed_time_and_budget(self) -> None:
        config = ClientConfig(
            host="127.0.0.1",
            https_port=1,
            http_only=True,
            ca_cert_file=Path("/unused"),
            session_cookie_path=self.session_file,
        )
        client = DroneApiClient(config)
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(DroneApiError):
                client.get("/admin/system-info", timeout=5)
        self.assertRegex(err.getvalue(), r"<- GET /admin/system-info FAILED after \d+\.\d+s \(timeout budget 5s\)")


if __name__ == "__main__":
    unittest.main()
