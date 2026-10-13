"""Issue #164 UAT: pairing under the flat ``python app/main.py`` entrypoint.

Reproduction: boot two Drones with ``python app/main.py``, then POST
``/v1/api/peer/pair`` with a valid pairing code. Before the fix the receiving
handler raised ``ModuleNotFoundError: No module named 'swarm_membership'``
after pinning the certificate and saving the peer, so the dialer saw HTTP 500
for a half-completed pairing.

Expected: the same request returns 200 ``paired``, records swarm membership,
and never logs that import error. Invalid pairing codes stay 403. Forget and
membership import fallbacks on the same entrypoint must not 500.
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
from urllib.parse import quote

from app.storage.state_store import database_path, load_payload


REPO_ROOT = Path(__file__).resolve().parents[2]
USERNAME = "admin"
PASSWORD = "changeme"
HARDWARE = {
    "alpha": ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", "BOARD-ALPHA-164"),
    "beta": ("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2", "BOARD-BETA-164"),
}


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


class Boot:
    def __init__(self, case: unittest.TestCase, workdir: Path, userdata: Path, hardware: Path, device_id: str):
        self.case = case
        self.userdata = userdata
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
        self.log.flush()
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def request(self, method: str, path: str, body=None, headers=None, port=None, raw=None):
        data = raw
        if data is None and body is not None:
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port or self.main_port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw_body = response.read()
                parsed = json.loads(raw_body) if raw_body else {}
                return response.status, parsed
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


def _peers(userdata: Path) -> dict:
    payload = load_payload(database_path(userdata), "local_paired_peers", {})
    return payload if isinstance(payload, dict) else {}


def _membership_ids(userdata: Path) -> set[str]:
    payload = load_payload(database_path(userdata), "swarm_membership", {}) or {}
    records = payload.get("records") if isinstance(payload, dict) else {}
    if not isinstance(records, dict):
        return set()
    return {
        peer_id
        for peer_id, record in records.items()
        if isinstance(record, dict) and record.get("op") == "add"
    }


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class FlatEntrypointPairingUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-164-pair-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.running: list[Boot] = []
        self.addCleanup(lambda: [boot.stop() for boot in self.running])
        self.hardware = {
            name: write_hardware(self.tmp / f"hw-{name}", *ids) for name, ids in HARDWARE.items()
        }

    def start(self, name: str) -> Boot:
        userdata = self.tmp / name
        userdata.mkdir()
        boot = Boot(self, self.tmp, userdata, self.hardware[name], device_id=f"drone-{name}-164")
        self.running.append(boot)
        return boot.wait_healthy()

    def assert_no_membership_import_error(self, boot: Boot) -> None:
        log = boot.text()
        self.assertNotIn("No module named 'swarm_membership'", log)
        self.assertNotIn('500 internal error "/v1/api/peer/pair"', log)
        self.assertNotIn('500 internal error "/v1/api/peer/membership"', log)
        self.assertNotIn("500 internal error \"/v1/api/admin/local-network/peers/", log)

    def test_valid_pairing_code_completes_under_flat_entrypoint(self) -> None:
        alpha = self.start("alpha")
        beta = self.start("beta")
        alpha_info = alpha.info()
        beta_info = beta.info()
        self.assertNotEqual(alpha_info["drone_id"], beta_info["drone_id"])

        status, payload = alpha.request(
            "POST",
            "/v1/api/peer/pair",
            {
                "pairing_code": "not-a-real-code",
                "drone_id": beta_info["drone_id"],
                "name": beta_info.get("name") or "beta",
                "certificate_pem": (
                    beta.userdata / "system" / "drone-app" / "certs" / "drone.crt"
                ).read_text(encoding="utf-8"),
                "certificate_fingerprint": beta_info["certificate_fingerprint"],
            },
            port=alpha.peer_port,
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(_peers(alpha.userdata), {})
        self.assertEqual(_membership_ids(alpha.userdata), set())

        status, payload = alpha.request(
            "POST",
            "/v1/api/peer/pair",
            raw=b"[1,2,3]",
            port=alpha.peer_port,
        )
        self.assertEqual(status, 400, payload)

        status, payload = alpha.request(
            "POST",
            "/v1/api/peer/pair",
            {
                "pairing_code": alpha.pairing_code(),
                "drone_id": "",
                "certificate_pem": (
                    beta.userdata / "system" / "drone-app" / "certs" / "drone.crt"
                ).read_text(encoding="utf-8"),
            },
            port=alpha.peer_port,
        )
        self.assertEqual(status, 400, payload)
        self.assertIn("invalid peer id", str(payload.get("error") or "").lower())

        code = alpha.pairing_code()
        pem = (beta.userdata / "system" / "drone-app" / "certs" / "drone.crt").read_text(encoding="utf-8")
        status, payload = alpha.request(
            "POST",
            "/v1/api/peer/pair",
            {
                "pairing_code": code,
                "drone_id": beta_info["drone_id"],
                "name": "Beta Cabinet",
                "hostname": "beta.local",
                "scheme": "http",
                "api_port": beta.main_port,
                "peer_mtls_port": beta.peer_port,
                "certificate_pem": pem,
                "certificate_fingerprint": beta_info["certificate_fingerprint"],
            },
            port=alpha.peer_port,
        )
        self.assertEqual(status, 200, msg=f"{payload}\n{alpha.text()[-2000:]}")
        self.assertEqual(payload.get("status"), "paired")
        self.assertEqual(payload.get("drone_id"), alpha_info["drone_id"])
        self.assertTrue(payload.get("certificate_pem"))

        peers = _peers(alpha.userdata)
        self.assertIn(beta_info["drone_id"], peers)
        self.assertEqual(
            peers[beta_info["drone_id"]]["certificate_fingerprint"],
            beta_info["certificate_fingerprint"],
        )
        cert_path = alpha.userdata / "system" / "drone-app" / "local-peer-certs" / f"{beta_info['drone_id']}.crt"
        # The pin file is named from a sanitised peer id; accept any pin file.
        pins = list((alpha.userdata / "system" / "drone-app" / "local-peer-certs").glob("*.crt"))
        self.assertTrue(pins or cert_path.exists(), f"expected pinned cert, got {pins}")
        self.assertIn(beta_info["drone_id"], _membership_ids(alpha.userdata))

        reused, reused_payload = alpha.request(
            "POST",
            "/v1/api/peer/pair",
            {
                "pairing_code": code,
                "drone_id": "aa:bb:cc:dd:ee:ff",
                "name": "replay",
                "certificate_pem": pem,
                "certificate_fingerprint": beta_info["certificate_fingerprint"],
            },
            port=alpha.peer_port,
        )
        self.assertEqual(reused, 403, reused_payload)

        membership_status, membership_payload = alpha.request(
            "POST",
            "/v1/api/peer/membership",
            {"records": [], "introducer_id": "stranger"},
            port=alpha.peer_port,
        )
        self.assertEqual(membership_status, 403, membership_payload)

        forget_path = f"/v1/api/admin/local-network/peers/{quote(beta_info['drone_id'], safe='')}/forget"
        forget_status, forget_payload = alpha.request(
            "POST",
            forget_path,
            {},
            headers=alpha.login(),
        )
        self.assertEqual(forget_status, 200, forget_payload)
        self.assertEqual(forget_payload.get("status"), "forgotten")
        self.assertNotIn(beta_info["drone_id"], _peers(alpha.userdata))

        missing_status, missing_payload = alpha.request(
            "POST",
            forget_path,
            {},
            headers=alpha.login(),
        )
        self.assertEqual(missing_status, 200, missing_payload)
        self.assertEqual(missing_payload.get("status"), "not_found")

        self.assert_no_membership_import_error(alpha)
        self.assert_no_membership_import_error(beta)

    def test_operator_pair_by_address_uses_the_same_flat_mode_receiver(self) -> None:
        alpha = self.start("alpha")
        beta = self.start("beta")
        code = alpha.pairing_code()
        status, payload = beta.request(
            "POST",
            "/v1/api/admin/local-network/pair-by-address",
            {"address": f"http://127.0.0.1:{alpha.main_port}", "pairing_code": code},
            headers=beta.login(),
        )
        self.assertEqual(status, 200, msg=f"{payload}\n{alpha.text()[-2000:]}\n{beta.text()[-2000:]}")
        self.assertEqual(payload.get("status"), "paired")
        alpha_id = alpha.info()["drone_id"]
        beta_id = beta.info()["drone_id"]
        self.assertIn(alpha_id, _peers(beta.userdata))
        self.assertIn(beta_id, _peers(alpha.userdata))
        self.assertIn(beta_id, _membership_ids(alpha.userdata))
        self.assertIn(alpha_id, _membership_ids(beta.userdata))
        self.assert_no_membership_import_error(alpha)
        self.assert_no_membership_import_error(beta)
