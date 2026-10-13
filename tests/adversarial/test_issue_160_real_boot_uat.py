"""Issue #160 end-to-end UAT: boot the real ``app/main.py`` on imaged disks.

Every scenario starts the actual entrypoint as a subprocess against a temporary
userdata tree, with the hardware report pointed at a fixture tree through
``DRONE_HARDWARE_ID_ROOT`` (emulated physical machines). Nothing is mocked, so this
covers the real startup order: identity validation must finish before any listener
exists, a cloned disk must come up as a new unpaired Drone, and unresolved evidence must
keep every peer surface closed.
"""

import hashlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UUID_A = "3f713284-3270-4c5f-990c-17231045b68e"
UUID_C = "b81d7e20-4c11-4f6a-9a53-6e0c2d98a417"
SERIAL_A = "BOARD-ORIGINAL-8941"
SERIAL_C = "BOARD-CLONEHOST-0077"
PEER_PATHS = ("/v1/api/peer/info", "/peer/info")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def write_hardware(root: Path, uuid: str = "", serial: str = "") -> Path:
    directory = root / "sys" / "class" / "dmi" / "id"
    directory.mkdir(parents=True, exist_ok=True)
    if uuid:
        (directory / "product_uuid").write_text(uuid + "\n", encoding="utf-8")
    if serial:
        (directory / "board_serial").write_text(serial + "\n", encoding="utf-8")
    return root


def fingerprint(userdata: Path) -> str:
    pem = (userdata / "system" / "drone-app" / "certs" / "drone.crt").read_text(encoding="utf-8")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


class Boot:
    """One run of the real Drone process."""

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
            "ROM_METADATA_POLL_SECONDS": "0",
            "DRONE_AUTO_UPDATE_POLL_SECONDS": "0",
            "DRONE_APP_USERNAME": "admin",
            "DRONE_APP_PASSWORD": "changeme",
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

    def request(self, port: int, method: str, path: str, body=None, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def health_id(self) -> str:
        return json.loads(self.request(self.main_port, "GET", "/health")[1])["drone_id"]

    def peer_port_open(self) -> bool:
        try:
            socket.create_connection(("127.0.0.1", self.peer_port), timeout=2).close()
            return True
        except OSError:
            return False

    def login(self) -> dict:
        status, _body, headers = self.request(
            self.main_port, "POST", "/v1/api/auth/login", {"username": "admin", "password": "changeme"}
        )
        self.case.assertEqual(status, 200)
        return {"Cookie": headers["Set-Cookie"].split(";")[0]}


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class RealBootCloneUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-160-boot-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hw_a = write_hardware(self.tmp / "hw-a", UUID_A, SERIAL_A)
        self.hw_c = write_hardware(self.tmp / "hw-c", UUID_C, SERIAL_C)
        (self.tmp / "hw-blind").mkdir()
        self.disk_a = self.tmp / "disk-a"
        self.disk_a.mkdir()

    def boot(self, userdata: Path, hardware: Path) -> Boot:
        return Boot(self, self.tmp, userdata, hardware)

    def image(self, name: str) -> Path:
        target = self.tmp / name
        shutil.copytree(self.disk_a, target, symlinks=True)
        return target

    def test_install_reboot_clone_and_unresolved_evidence_on_the_real_entrypoint(self) -> None:
        # Scenario A: first boot creates the identity, a reboot changes nothing.
        with self.boot(self.disk_a, self.hw_a) as first:
            original_id = first.health_id()
            first_log = first.text()
        original_fp = fingerprint(self.disk_a)
        identity_dir = self.disk_a / "system" / "drone-app" / "identity"
        self.assertIn("[Identity] Provisioning a new installation identity", first_log)
        self.assertLess(first_log.index("[Identity] Swarm networking enabled"), first_log.index("Serving Drone peer-mTLS listener"))
        before = {
            "cert": (self.disk_a / "system/drone-app/certs/drone.crt").read_bytes(),
            "key": (self.disk_a / "system/drone-app/certs/drone.key").read_bytes(),
            "binding": (identity_dir / "binding.json").read_bytes(),
        }
        with self.boot(self.disk_a, self.hw_a) as again:
            self.assertEqual(again.health_id(), original_id)
            reboot_log = again.text()
        self.assertIn("[Identity] Existing installation verified", reboot_log)
        self.assertNotIn("Clone", reboot_log)
        self.assertEqual((self.disk_a / "system/drone-app/certs/drone.crt").read_bytes(), before["cert"])
        self.assertEqual((self.disk_a / "system/drone-app/certs/drone.key").read_bytes(), before["key"])
        self.assertEqual((identity_dir / "binding.json").read_bytes(), before["binding"])
        # Mark the original's swarm trust so a clone leaking it is visible.
        pins = self.disk_a / "system" / "drone-app" / "local-peer-certs"
        pins.mkdir(parents=True, exist_ok=True)
        (pins / "friend.crt").write_text("-----BEGIN CERTIFICATE-----\npinned\n", encoding="utf-8")

        # Scenario B/D/E: images of the same disk on two different machines.
        clones = []
        for name, serial in (("clone-1", SERIAL_C), ("clone-2", "BOARD-ANOTHERBOX-3310")):
            hardware = write_hardware(self.tmp / f"hw-{name}", f"{name[-1]}0c2a8d4-11aa-4b2c-8d3e-7f6a5b4c3d2e", serial)
            userdata = self.image(name)
            with self.boot(userdata, hardware) as clone:
                clone_id = clone.health_id()
                clone_log = clone.text()
                # Peer surface exists again, but only for the new, unpaired identity.
                self.assertEqual(clone.request(clone.peer_port, "GET", "/v1/api/peer/info")[0], 200)
            self.assertNotEqual(clone_id, original_id)
            self.assertNotEqual(fingerprint(userdata), original_fp)
            self.assertEqual(list((userdata / "system/drone-app/local-peer-certs").glob("*")), [])
            for marker in (
                "[Identity] High-confidence hardware mismatch detected",
                "[Identity] Clone reprovisioning initiated",
                "[Identity] New cryptographic identity created",
                "[Identity] Inherited swarm trust cleared",
                "[Identity] Clone provisioning completed",
                "[Identity] Swarm networking enabled",
            ):
                self.assertIn(marker, clone_log)
            self.assertLess(clone_log.index("Clone provisioning completed"), clone_log.index("Serving Drone peer-mTLS listener"))
            for secret in ("PRIVATE KEY", UUID_A, SERIAL_A, SERIAL_C, serial):
                self.assertNotIn(secret, clone_log)
            clones.append((clone_id, fingerprint(userdata)))
        self.assertEqual(len({clones[0][0], clones[1][0], original_id}), 3)
        self.assertEqual(len({clones[0][1], clones[1][1], original_fp}), 3)

        # Scenario C: the original is untouched and still has its pin.
        self.assertEqual(fingerprint(self.disk_a), original_fp)
        self.assertTrue((pins / "friend.crt").exists())
        with self.boot(self.disk_a, self.hw_a) as original:
            self.assertEqual(original.health_id(), original_id)

        # Missing evidence on a bound install: nothing rotates, every peer surface stays closed.
        held_disk = self.image("held")
        with self.boot(held_disk, self.tmp / "hw-blind") as held:
            held_log = held.text()
            self.assertEqual(held.health_id(), original_id)
            self.assertFalse(held.peer_port_open(), msg="the peer mTLS listener must not exist while identity is unresolved")
            for path in PEER_PATHS:
                self.assertEqual(held.request(held.main_port, "GET", path)[0], 503, msg=path)
            self.assertEqual(held.request(held.main_port, "POST", "/v1/api/peer/pair-request", {})[0], 503)
            status, body, _ = held.request(held.main_port, "GET", "/v1/api/admin/identity/status", headers=held.login())
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["state"], "RECOVERY_REQUIRED")
            self.assertFalse(payload["swarm_networking_enabled"])
            self.assertNotIn(UUID_A, body.decode())
        self.assertNotIn("Serving Drone peer-mTLS listener", held_log)
        self.assertIn("Identity recovery required", held_log)
        self.assertEqual(fingerprint(held_disk), original_fp)
        self.assertTrue((held_disk / "system/drone-app/local-peer-certs/friend.crt").exists())


if __name__ == "__main__":
    unittest.main()
