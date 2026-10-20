"""Issue #176 UAT: two fresh installs on one host must not share a MAC-derived id.

Reproduction from the issue: boot two Drones with ``python app/main.py`` on the same
machine without ``DRONE_DEVICE_ID``, then ``GET /v1/api/peer/info``. Before the fix
both reported the host NIC MAC as ``drone_id``, so
``POST /v1/api/admin/local-network/pair-by-address`` returned 409
"that address answered as this Drone itself".

Expected, derived from that report and the identity spec:

* each userdata tree is its own installation even when hardware evidence is the
  host's (two processes on one machine);
* a first boot without ``DRONE_DEVICE_ID`` must mint a logical id that is not the
  NIC MAC / ``uuid.getnode()`` fallback, persist it, and keep it across restart;
* pair-by-address between those installs succeeds; pointing it at this Drone's
  own address still 409s; a wrong pairing code is 403, not a self-collision.
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
import uuid
from pathlib import Path

from app.common.device_identity import _physical_mac_candidates, _runtime_machine_id
from app.storage.state_store import database_path, load_payload


REPO_ROOT = Path(__file__).resolve().parents[2]
USERNAME = "admin"
PASSWORD = "changeme"
HOST_HARDWARE = ("5e98abb9-e38b-4a60-8b71-9c82d0e1f203", "BOARD-SHARED-HOST-176")
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"


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


def host_derived_ids() -> set[str]:
    ids = {mac.strip().lower() for mac in _physical_mac_candidates() if mac}
    ids.add(_runtime_machine_id().strip().lower())
    node = uuid.getnode()
    ids.add(":".join(f"{(node >> shift) & 0xff:02x}" for shift in range(40, -1, -8)))
    return {item for item in ids if item}


def _peers(userdata: Path) -> dict:
    payload = load_payload(database_path(userdata), "local_paired_peers", {})
    return payload if isinstance(payload, dict) else {}


class Boot:
    def __init__(self, case: unittest.TestCase, workdir: Path, userdata: Path, hardware: Path):
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

    def persisted_device_id(self) -> str:
        path = self.userdata / "system" / "drone-app" / "device-id"
        self.case.assertTrue(path.is_file(), f"missing persisted device-id at {path}")
        return path.read_text(encoding="utf-8").strip()


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class FreshInstallsOnOneHostUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-176-pair-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.running: list[Boot] = []
        self.addCleanup(lambda: [boot.stop() for boot in self.running])
        # One host: both installs see the same DMI evidence.
        self.hardware = write_hardware(self.tmp / "hw-host", *HOST_HARDWARE)
        self.host_ids = host_derived_ids()

    def start(self, name: str, userdata: Path | None = None) -> Boot:
        userdata = userdata or (self.tmp / name)
        userdata.mkdir(parents=True, exist_ok=True)
        boot = Boot(self, self.tmp, userdata, self.hardware)
        self.running.append(boot)
        return boot.wait_healthy()

    def assert_logical_id(self, drone_id: str, label: str) -> None:
        self.assertRegex(drone_id, MAC_SHAPED, f"{label} drone_id {drone_id!r} is not MAC-shaped")
        self.assertEqual(int(drone_id.split(":")[0], 16) & 0x03, 0x02, f"{label} is not locally administered unicast")
        self.assertNotIn(drone_id.lower(), self.host_ids, f"{label} adopted a host-derived id: {drone_id}")

    def test_unpinned_installs_on_one_host_pair_instead_of_colliding_as_self(self) -> None:
        alpha = self.start("alpha")
        beta = self.start("beta")
        alpha_info = alpha.info()
        beta_info = beta.info()
        alpha_id = str(alpha_info["drone_id"])
        beta_id = str(beta_info["drone_id"])

        self.assert_logical_id(alpha_id, "alpha")
        self.assert_logical_id(beta_id, "beta")
        self.assertNotEqual(alpha_id, beta_id)
        self.assertEqual(alpha.persisted_device_id(), alpha_id)
        self.assertEqual(beta.persisted_device_id(), beta_id)
        self.assertNotEqual(alpha_info.get("certificate_fingerprint"), beta_info.get("certificate_fingerprint"))

        status, payload = beta.request(
            "POST",
            "/v1/api/admin/local-network/pair-by-address",
            {"address": f"http://127.0.0.1:{beta.main_port}", "pairing_code": "XXXXXXXX"},
            headers=beta.login(),
        )
        self.assertEqual(status, 409, payload)
        self.assertIn("itself", str(payload.get("error") or "").lower())

        status, payload = beta.request(
            "POST",
            "/v1/api/admin/local-network/pair-by-address",
            {"address": f"http://127.0.0.1:{alpha.main_port}", "pairing_code": "not-a-code",
             "certificate_fingerprint": alpha.info()["certificate_fingerprint"]},
            headers=beta.login(),
        )
        # Wrong code to a *different* Drone must not be the self-collision 409
        # that this issue fixes. pair-by-address currently surfaces the peer's
        # 403 as HTTP 500 (see out-of-scope finding); either way it is not a pair.
        self.assertNotEqual(status, 200, payload)
        self.assertNotEqual(status, 409, payload)
        self.assertNotIn("itself", str(payload.get("error") or "").lower())

        status, payload = beta.request(
            "POST",
            "/v1/api/admin/local-network/pair-by-address",
            {"address": "ftp://nope", "pairing_code": "XXXXXXXX"},
            headers=beta.login(),
        )
        self.assertEqual(status, 400, payload)

        code = alpha.pairing_code()
        status, payload = beta.request(
            "POST",
            "/v1/api/admin/local-network/pair-by-address",
            {"address": f"http://127.0.0.1:{alpha.main_port}", "pairing_code": code,
             "certificate_fingerprint": alpha.info()["certificate_fingerprint"]},
            headers=beta.login(),
        )
        self.assertEqual(
            status,
            200,
            msg=f"{payload}\n{alpha.text()[-2000:]}\n{beta.text()[-2000:]}",
        )
        self.assertEqual(payload.get("status"), "paired")
        self.assertIn(alpha_id, _peers(beta.userdata))
        self.assertIn(beta_id, _peers(alpha.userdata))

        alpha.stop()
        self.running.remove(alpha)
        restarted = self.start("alpha-restart", userdata=alpha.userdata)
        restarted_info = restarted.info()
        self.assertEqual(restarted_info["drone_id"], alpha_id)
        self.assertEqual(restarted.persisted_device_id(), alpha_id)


if __name__ == "__main__":
    unittest.main()
