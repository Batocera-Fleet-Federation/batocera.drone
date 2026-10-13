"""Issue #163 real-entrypoint UAT: imaged disk boots and rejects captured admin tokens.

Boots ``app/main.py`` against a userdata tree, captures a live session cookie and
MCP bearer from the source, restores the tree onto different hardware, and
checks the clone's HTTP surfaces. Login credentials from the image still work.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path

from app.common.auth import SESSION_COOKIE_NAME, SessionStore
from app.storage.state_store import database_path, open_database


REPO_ROOT = Path(__file__).resolve().parents[2]
UUID_A = "3f713284-3270-4c5f-990c-17231045b68e"
UUID_C = "b81d7e20-4c11-4f6a-9a53-6e0c2d98a417"
SERIAL_A = "BOARD-ORIGINAL-8941"
SERIAL_C = "BOARD-CLONEHOST-0077"
USERNAME = "admin"
PASSWORD = "changeme"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def write_hardware(root: Path, uuid: str, serial: str) -> Path:
    directory = root / "sys" / "class" / "dmi" / "id"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "product_uuid").write_text(uuid + "\n", encoding="utf-8")
    (directory / "board_serial").write_text(serial + "\n", encoding="utf-8")
    return root


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


def image_disk(source: Path, target: Path) -> None:
    with open_database(database_path(source)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copytree(source, target, symlinks=True)


class Boot:
    def __init__(self, case: unittest.TestCase, workdir: Path, userdata: Path, hardware: Path):
        self.case = case
        self.userdata = userdata
        self.main_port = free_port()
        self.peer_port = free_port()
        self.log_path = workdir / f"boot-{time.monotonic_ns()}.log"
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(workdir),
            "USERDATA_ROOT": str(userdata),
            "LOG_DIR": str(workdir / "logs"),
            "TLS_SELF_SIGNED_DIR": str(userdata / "system" / "certs"),
            "HTTP_ONLY": "1",
            "HTTPS_PORT": str(self.main_port),
            "DRONE_PEER_MTLS_PORT": str(self.peer_port),
            "DRONE_COMPAT_HTTPS_PORTS": "",
            "HTTP_REDIRECT_PORT": "0",
            "DRONE_CAST_ENABLED": "0",
            "DRONE_CAST_HTTP_PORT": str(free_port()),
            "ROM_METADATA_POLL_SECONDS": "0",
            "DRONE_AUTO_UPDATE_POLL_SECONDS": "0",
            "DRONE_APP_USERNAME": USERNAME,
            "DRONE_APP_PASSWORD": PASSWORD,
            "DRONE_HARDWARE_ID_ROOT": str(hardware),
            "PYTHONUNBUFFERED": "1",
        }
        self.log = open(self.log_path, "wb")
        self.process = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "app" / "main.py")],
            cwd=str(workdir),
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )

    def __enter__(self) -> "Boot":
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.case.fail(f"Drone exited early ({self.process.returncode}):\n{self.text()}")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.main_port}/health", timeout=2).read()
                return self
            except (OSError, urllib.error.URLError):
                time.sleep(0.3)
        self.case.fail(f"Drone did not become healthy:\n{self.text()}")

    def __exit__(self, *_exc) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.log.close()

    def text(self) -> str:
        self.log.flush()
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def request(self, method: str, path: str, body=None, headers=None, host: str = "127.0.0.1"):
        request = urllib.request.Request(
            f"http://{host}:{self.main_port}{path}",
            data=None if body is None else json.dumps(body).encode("utf-8"),
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

    def login(self) -> str:
        status, body, headers = self.request(
            "POST", "/v1/api/auth/login", {"username": USERNAME, "password": PASSWORD}
        )
        self.case.assertEqual(status, 200, body)
        jar = SimpleCookie()
        jar.load(headers["Set-Cookie"])
        return f"{SESSION_COOKIE_NAME}={jar[SESSION_COOKIE_NAME].value}"


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class CloneAuthRealBootUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-163-boot-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hw_a = write_hardware(self.tmp / "hw-a", UUID_A, SERIAL_A)
        self.hw_c = write_hardware(self.tmp / "hw-c", UUID_C, SERIAL_C)
        self.disk_a = self.tmp / "disk-a"
        self.disk_a.mkdir()

    def boot(self, userdata: Path, hardware: Path) -> Boot:
        return Boot(self, self.tmp, userdata, hardware)

    def test_imaged_clone_boot_rejects_captured_session_and_mcp_token(self) -> None:
        with self.boot(self.disk_a, self.hw_a) as source:
            cookie = source.login()
            status, created, _headers = source.request(
                "POST", "/v1/api/admin/mcp/token", {"label": "source boot capture"}, headers={"Cookie": cookie}
            )
            self.assertEqual(status, 200, created)
            mcp_token = created["token"]
            status, listed, _headers = source.request(
                "POST",
                "/v1/api/mcp",
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Authorization": f"Bearer {mcp_token}"},
            )
            self.assertEqual(status, 200)
            self.assertIn("tools", listed["result"])

        clone_disk = self.tmp / "disk-clone"
        image_disk(self.disk_a, clone_disk)

        with self.boot(clone_disk, self.hw_c) as clone:
            self.assertIn("[Identity] Clone provisioning completed", clone.text())
            cookie_token = cookie.split("=", 1)[1]
            self.assertIsNone(SessionStore(database_path(clone_disk)).validate(cookie_token))
            remote = non_loopback_host()
            if remote:
                status, session_body, _headers = clone.request(
                    "GET", "/v1/api/auth/session", headers={"Cookie": cookie}, host=remote
                )
                self.assertEqual(status, 200)
                self.assertFalse(session_body["authenticated"])
                status, _body, _headers = clone.request(
                    "GET", "/v1/api/admin/mcp", headers={"Cookie": cookie}, host=remote
                )
                self.assertEqual(status, 401)
            status, mcp_err, _headers = clone.request(
                "POST",
                "/v1/api/mcp",
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Authorization": f"Bearer {mcp_token}"},
            )
            self.assertEqual(status, 401)
            self.assertEqual(mcp_err["error"], "invalid or missing MCP bearer token")

            fresh_cookie = clone.login()
            status, created, _headers = clone.request(
                "POST", "/v1/api/admin/mcp/token", {"label": "clone boot"}, headers={"Cookie": fresh_cookie}
            )
            self.assertEqual(status, 200, created)
            clone_token = created["token"]
            self.assertNotEqual(clone_token, mcp_token)
            status, listed, _headers = clone.request(
                "POST",
                "/v1/api/mcp",
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers={"Authorization": f"Bearer {clone_token}"},
            )
            self.assertEqual(status, 200)
            self.assertIn("tools", listed["result"])

        with self.boot(self.disk_a, self.hw_a) as source_again:
            cookie_token = cookie.split("=", 1)[1]
            self.assertEqual(SessionStore(database_path(self.disk_a)).validate(cookie_token), USERNAME)
            remote = non_loopback_host()
            if remote:
                status, session_body, _headers = source_again.request(
                    "GET", "/v1/api/auth/session", headers={"Cookie": cookie}, host=remote
                )
                self.assertEqual(status, 200)
                self.assertTrue(session_body["authenticated"])
            status, listed, _headers = source_again.request(
                "POST",
                "/v1/api/mcp",
                {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                headers={"Authorization": f"Bearer {mcp_token}"},
            )
            self.assertEqual(status, 200)
            self.assertIn("tools", listed["result"])


if __name__ == "__main__":
    unittest.main()
