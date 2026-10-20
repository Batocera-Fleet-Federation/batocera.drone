"""Issue #198 UAT: pair-by-address must not surface a peer 403 as HTTP 500.

Reproduction from the issue: boot two unpinned Drones with distinct device ids,
login on B, POST /v1/api/admin/local-network/pair-by-address with A's URL and
pairing_code ``not-a-code``. Before the fix the initiator answered HTTP 500
``{"error": "HTTP Error 403: Forbidden"}`` because urllib's HTTPError escaped
the handler.

Expected, derived from that report and the pair-by-address contract:

* GET /v1/api/peer/info succeeding then POST /v1/api/peer/pair returning 403
  (wrong / empty / expired pairing code) is an upstream rejection, not a local
  bug. The admin endpoint already maps a failed identity fetch to 502; the
  handshake must use the same gateway class (502), never 500.
* The operator-facing body is JSON and names the peer rejection (HTTP 403) plus
  the peer's ``invalid or expired pairing code`` text. It is not urllib's raw
  ``HTTP Error 403: Forbidden`` string.
* A rejected attempt stores no paired-peer row on either side. A later attempt
  with a valid code still pairs.
* Pointing the same endpoint at this Drone remains 409; an unparseable address
  remains 400; an address with nothing listening remains 502.
"""

from __future__ import annotations

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
from pathlib import Path

from app.storage.state_store import database_path, load_payload


REPO_ROOT = Path(__file__).resolve().parents[2]
USERNAME = "admin"
PASSWORD = "changeme"
HARDWARE = {
    "alpha": ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaa198", "BOARD-ALPHA-198"),
    "beta": ("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbb198", "BOARD-BETA-198"),
}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def write_hardware(root: Path, product_uuid: str, serial: str) -> Path:
    directory = root / "sys" / "class" / "dmi" / "id"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "product_uuid").write_text(product_uuid + "\n", encoding="utf-8")
    (directory / "board_serial").write_text(serial + "\n", encoding="utf-8")
    return root


def _peers(userdata: Path) -> dict:
    payload = load_payload(database_path(userdata), "local_paired_peers", {})
    return payload if isinstance(payload, dict) else {}


class Boot:
    def __init__(
        self,
        case: unittest.TestCase,
        workdir: Path,
        userdata: Path,
        hardware: Path,
        device_id: str,
    ):
        self.case = case
        self.userdata = userdata
        self.device_id = device_id
        self.main_port = free_port()
        self.peer_port = free_port()
        self.log_path = workdir / f"{userdata.name}-{time.monotonic_ns()}.log"
        for folder in ("roms", "bios", "saves"):
            (userdata / folder).mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(workdir),
            "USERDATA_ROOT": str(userdata),
            "ROMS_ROOT": str(userdata / "roms"),
            "BIOS_ROOT": str(userdata / "bios"),
            "SAVES_ROOT": str(userdata / "saves"),
            "LOG_DIR": str(workdir / f"logs-{userdata.name}"),
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
            "DRONE_DEVICE_ID": device_id,
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

    def wait_healthy(self) -> "Boot":
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

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if not self.log.closed:
            self.log.close()

    def text(self) -> str:
        if not self.log.closed:
            self.log.flush()
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def request(self, method: str, path: str, body=None, headers=None, port=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port or self.main_port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw_body = response.read()
                return response.status, json.loads(raw_body) if raw_body else {}
        except urllib.error.HTTPError as error:
            raw_body = error.read()
            try:
                parsed = json.loads(raw_body) if raw_body else {}
            except ValueError:
                parsed = {"raw": raw_body.decode("utf-8", "replace")}
            return error.code, parsed

    def login(self) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.main_port}/v1/api/auth/login",
            data=json.dumps({"username": USERNAME, "password": PASSWORD}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.case.assertEqual(response.status, 200)
            cookie = response.headers["Set-Cookie"].split(";")[0]
        return {"Cookie": cookie}

    def info(self) -> dict:
        status, payload = self.request("GET", "/v1/api/peer/info")
        self.case.assertEqual(status, 200, payload)
        return payload

    def pairing_code(self) -> str:
        status, payload = self.request(
            "POST",
            "/v1/api/admin/local-network/pairing-code/rotate",
            {},
            headers=self.login(),
        )
        self.case.assertEqual(status, 200, payload)
        return str(payload["pairing"]["code"])

    def pair_by_address(self, address: str, pairing_code: str, fingerprint: str = "ab" * 32):
        return self.request(
            "POST",
            "/v1/api/admin/local-network/pair-by-address",
            {"address": address, "pairing_code": pairing_code, "certificate_fingerprint": fingerprint},
            headers=self.login(),
        )


def _assert_peer_reject_502(case: unittest.TestCase, status: int, payload: dict, logs: str) -> None:
    error = str(payload.get("error") or "")
    case.assertEqual(status, 502, msg=f"{payload}\n{logs}")
    case.assertNotEqual(status, 500, msg=f"{payload}\n{logs}")
    case.assertIsInstance(payload, dict)
    case.assertIn("error", payload)
    case.assertNotEqual(error.strip(), "HTTP Error 403: Forbidden")
    case.assertNotIn("Traceback", error)
    case.assertIn("403", error)
    case.assertIn("invalid or expired pairing code", error.lower())
    case.assertLess(len(error), 1000)


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class PairByAddressPeer403UAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-198-pair-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.running: list[Boot] = []
        self.addCleanup(lambda: [boot.stop() for boot in self.running])
        self.hardware = {
            name: write_hardware(self.tmp / f"hw-{name}", *ids) for name, ids in HARDWARE.items()
        }

    def start(self, name: str, device_id: str) -> Boot:
        userdata = self.tmp / name
        userdata.mkdir(parents=True, exist_ok=True)
        boot = Boot(self, self.tmp, userdata, self.hardware[name], device_id)
        self.running.append(boot)
        return boot.wait_healthy()

    def test_wrong_pairing_code_is_502_gateway_not_unhandled_500(self) -> None:
        alpha = self.start("alpha", "aa:aa:aa:aa:01:98")
        beta = self.start("beta", "bb:bb:bb:bb:01:98")
        alpha_info = alpha.info()
        beta_info = beta.info()
        alpha_url = f"http://127.0.0.1:{alpha.main_port}"
        logs = lambda: f"{alpha.text()[-2500:]}\n{beta.text()[-2500:]}"

        self.assertNotEqual(alpha_info["drone_id"], beta_info["drone_id"])

        status, payload = beta.pair_by_address(alpha_url, "not-a-code")
        _assert_peer_reject_502(self, status, payload, logs())
        self.assertNotIn(alpha_info["drone_id"], _peers(beta.userdata))
        self.assertNotIn(beta_info["drone_id"], _peers(alpha.userdata))

        status, payload = beta.pair_by_address(alpha_url, "")
        _assert_peer_reject_502(self, status, payload, logs())

        status, payload = beta.pair_by_address(f"http://127.0.0.1:{beta.main_port}", "XXXXXXXX")
        self.assertEqual(status, 409, payload)
        self.assertIn("itself", str(payload.get("error") or "").lower())

        status, payload = beta.pair_by_address("ftp://nope", "XXXXXXXX")
        self.assertEqual(status, 400, payload)

        status, payload = beta.pair_by_address("http://127.0.0.1:9", "XXXXXXXX")
        self.assertEqual(status, 502, payload)

        code = alpha.pairing_code()
        status, payload = beta.pair_by_address(alpha_url, code, alpha_info["certificate_fingerprint"])
        self.assertEqual(status, 200, msg=f"{payload}\n{logs()}")
        self.assertEqual(payload.get("status"), "paired")
        self.assertIn(alpha_info["drone_id"], _peers(beta.userdata))
        self.assertIn(beta_info["drone_id"], _peers(alpha.userdata))


if __name__ == "__main__":
    unittest.main()
