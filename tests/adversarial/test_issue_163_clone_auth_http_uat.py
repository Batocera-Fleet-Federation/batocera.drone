"""Issue #163 HTTP UAT: a captured cookie or MCP bearer must not authorize the clone.

A user who copied a disk image and boots it on new hardware is running a new
Drone. Login credentials from the image still work. A session cookie or MCP
token minted on the source must fail against the clone's HTTP surfaces, and
must keep working on the source.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.auth import DroneCredentialStore, SESSION_COOKIE_NAME, SessionStore
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.drone_api import create_server
from app.storage.state_store import database_path, open_database
from tests.test_hardware_identity import UUID_B, x86_tree


OPENSSL_AVAILABLE = shutil.which("openssl") is not None
USERNAME = "arcade-admin"
PASSWORD = "stored-secret-99"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def non_loopback_host() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            host = sock.getsockname()[0]
        if host and not ipaddress.ip_address(host).is_loopback:
            return host
    except OSError:
        return None
    return None


def settings_for(root: Path, **extra: str) -> Settings:
    for folder in ("roms", "bios", "saves"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
        "HTTP_ONLY": "1",
        "HTTPS_PORT": "0",
        "HTTP_REDIRECT_PORT": "0",
        "DRONE_COMPAT_HTTPS_PORTS": "",
        "DRONE_PEER_MTLS_PORT": str(free_port()),
        "DRONE_CAST_ENABLED": "0",
        "ROM_METADATA_POLL_SECONDS": "0",
        "DRONE_AUTO_UPDATE_POLL_SECONDS": "0",
        **extra,
    }
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings.from_env()


def provider_for(root: Path) -> HardwareIdentityProvider:
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def image_disk(source: Path, target: Path) -> None:
    with open_database(database_path(source)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copytree(source, target, symlinks=True)


class HttpDrone:
    def __init__(self, case: unittest.TestCase, settings: Settings) -> None:
        self.case = case
        try:
            self.server = create_server(settings)
        except PermissionError as error:
            case.skipTest(f"Socket bind is not allowed here: {error}")
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method: str, path: str, body=None, headers=None, host: str = "127.0.0.1"):
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://{host}:{self.port}{path}",
            data=payload,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None, response.headers
        except urllib.error.HTTPError as error:
            raw = error.read()
            parsed = None
            if raw:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = raw.decode("utf-8", errors="replace")
            return error.code, parsed, error.headers

    def login(self, username: str, password: str):
        status, body, headers = self.request(
            "POST", "/v1/api/auth/login", {"username": username, "password": password}
        )
        return status, body, headers


@unittest.skipUnless(OPENSSL_AVAILABLE, "openssl is required for identity provisioning")
class CloneAuthHttpUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-163-http-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hw_a = x86_tree(self.tmp / "hw-a")
        self.hw_b = x86_tree(self.tmp / "hw-b", uuid=UUID_B, board="BOARDSERIAL02")
        self.disk_a = self.tmp / "disk-a"
        self.disk_a.mkdir()

    def test_captured_cookie_and_mcp_bearer_are_rejected_on_the_clone_http_surface(self) -> None:
        original_settings = settings_for(self.disk_a)
        installed = identity.ensure_identity(original_settings, provider_for(self.hw_a)).settings
        DroneCredentialStore(
            installed.credentials_file,
            env_username="env-user",
            env_password="env-password-fallback",
            state_database_file=database_path(installed.userdata_root),
        ).update(USERNAME, PASSWORD)

        original_http = HttpDrone(self, installed)
        try:
            status, body, headers = original_http.login(USERNAME, PASSWORD)
            self.assertEqual(status, 200)
            self.assertEqual(body["username"], USERNAME)
            jar = SimpleCookie()
            jar.load(headers["Set-Cookie"])
            cookie_token = jar[SESSION_COOKIE_NAME].value
            cookie_header = f"{SESSION_COOKIE_NAME}={cookie_token}"

            status, mcp_body, _headers = original_http.request(
                "POST",
                "/v1/api/admin/mcp/token",
                {"label": "source capture"},
                headers={"Cookie": cookie_header},
            )
            self.assertEqual(status, 200)
            mcp_token = mcp_body["token"]
            self.assertTrue(str(mcp_token).startswith("dmcp_"))

            status, session_body, _headers = original_http.request(
                "GET", "/v1/api/auth/session", headers={"Cookie": cookie_header}
            )
            self.assertEqual(status, 200)
            self.assertTrue(session_body["authenticated"])
            status, mcp_ok, _headers = original_http.request(
                "POST",
                "/v1/api/mcp",
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Authorization": f"Bearer {mcp_token}"},
            )
            self.assertEqual(status, 200)
            self.assertIn("tools", mcp_ok["result"])
        finally:
            original_http.close()

        clone_root = self.tmp / "disk-b"
        image_disk(self.disk_a, clone_root)
        identity.reset_runtime_state_for_tests()
        clone_settings = settings_for(clone_root)
        clone_result = identity.ensure_identity(clone_settings, provider_for(self.hw_b))
        self.assertEqual(clone_result.status, identity.STATUS_CLONE)
        clone_http = HttpDrone(self, clone_result.settings)
        try:
            cookie_header = f"{SESSION_COOKIE_NAME}={cookie_token}"
            self.assertIsNone(SessionStore(database_path(clone_result.settings.userdata_root)).validate(cookie_token))

            status, mcp_err, _headers = clone_http.request(
                "POST",
                "/v1/api/mcp",
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Authorization": f"Bearer {mcp_token}"},
            )
            self.assertEqual(status, 401)
            self.assertEqual(mcp_err["error"], "invalid or missing MCP bearer token")

            status, _body, _headers = clone_http.request(
                "POST",
                "/v1/api/mcp",
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Authorization": f"Bearer {cookie_token}"},
            )
            self.assertEqual(status, 401)

            remote = non_loopback_host()
            if remote:
                status, session_body, _headers = clone_http.request(
                    "GET", "/v1/api/auth/session", headers={"Cookie": cookie_header}, host=remote
                )
                self.assertEqual(status, 200)
                self.assertFalse(session_body["authenticated"])
                status, _body, _headers = clone_http.request(
                    "GET", "/v1/api/admin/mcp", headers={"Cookie": cookie_header}, host=remote
                )
                self.assertEqual(status, 401)
                status, _body, _headers = clone_http.request(
                    "GET",
                    "/v1/api/admin/mcp",
                    headers={"Cookie": f"{SESSION_COOKIE_NAME}={mcp_token}"},
                    host=remote,
                )
                self.assertEqual(status, 401)

            status, _body, _headers = clone_http.login("env-user", "env-password-fallback")
            self.assertEqual(status, 401)
            status, login_body, login_headers = clone_http.login(USERNAME, PASSWORD)
            self.assertEqual(status, 200)
            self.assertEqual(login_body["username"], USERNAME)
            fresh_jar = SimpleCookie()
            fresh_jar.load(login_headers["Set-Cookie"])
            fresh_cookie = f"{SESSION_COOKIE_NAME}={fresh_jar[SESSION_COOKIE_NAME].value}"

            status, created, _headers = clone_http.request(
                "POST",
                "/v1/api/admin/mcp/token",
                {"label": "clone replacement"},
                headers={"Cookie": fresh_cookie},
            )
            self.assertEqual(status, 200)
            clone_token = created["token"]
            self.assertNotEqual(clone_token, mcp_token)
            status, listed, _headers = clone_http.request(
                "POST",
                "/v1/api/mcp",
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers={"Authorization": f"Bearer {clone_token}"},
            )
            self.assertEqual(status, 200)
            self.assertIn("tools", listed["result"])
            status, _body, _headers = clone_http.request(
                "POST",
                "/v1/api/mcp",
                {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                headers={"Authorization": f"Bearer {mcp_token}"},
            )
            self.assertEqual(status, 401)
        finally:
            clone_http.close()

        identity.reset_runtime_state_for_tests()
        source_again = identity.ensure_identity(settings_for(self.disk_a), provider_for(self.hw_a))
        self.assertEqual(source_again.status, identity.STATUS_VERIFIED)
        source_http = HttpDrone(self, source_again.settings)
        try:
            cookie_header = f"{SESSION_COOKIE_NAME}={cookie_token}"
            self.assertEqual(
                SessionStore(database_path(source_again.settings.userdata_root)).validate(cookie_token),
                USERNAME,
            )
            remote = non_loopback_host()
            if remote:
                status, session_body, _headers = source_http.request(
                    "GET", "/v1/api/auth/session", headers={"Cookie": cookie_header}, host=remote
                )
                self.assertEqual(status, 200)
                self.assertTrue(session_body["authenticated"])
            status, listed, _headers = source_http.request(
                "POST",
                "/v1/api/mcp",
                {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                headers={"Authorization": f"Bearer {mcp_token}"},
            )
            self.assertEqual(status, 200)
            self.assertIn("tools", listed["result"])
            status, _body, _headers = source_http.request(
                "POST",
                "/v1/api/mcp",
                {"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
                headers={"Authorization": f"Bearer {clone_token}"},
            )
            self.assertEqual(status, 401)
        finally:
            source_http.close()


if __name__ == "__main__":
    unittest.main()
