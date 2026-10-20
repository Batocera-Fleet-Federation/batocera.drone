"""Issue #160 end-to-end swarm UAT: an original Drone and two clones of its disk image.

Starts the real application (``python -m app.main``, the way the Batocera service starts it)
as three concurrent processes on emulated physical machines and drives the real pairing API:

* every clone comes up as its own independent Drone (distinct id, distinct certificate that
  its listener actually serves) with none of the original's swarm state;
* clones join the original's swarm through the normal pairing flow, each pinning the
  original's real certificate while the original pins each clone's *new* certificate;
* the original keeps its pre-existing peer and never receives a removal for itself;
* a pairing request that presents the original's own certificate (what an un-reprovisioned
  copy would send) is refused as a duplicate identity and changes nothing.
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
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FRIEND_ID = "de:ad:be:ef:00:01"
FRIEND_FINGERPRINT = "ab" * 32
HARDWARE = {
    "original": ("6a1c2d3e-4b5f-4a60-8b71-9c82d0e1f203", "ORIGINALBOARD-0001"),
    "clone-1": ("7b2d3e4f-5c60-4b71-9c82-ad93e1f20314", "CLONEBOARD-0002"),
    "clone-2": ("8c3e4f50-6d71-4c82-ad93-be04f2031425", "CLONEBOARD-0003"),
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


def disk_fingerprint(userdata: Path) -> str:
    pem = (userdata / "system" / "drone-app" / "certs" / "drone.crt").read_text(encoding="utf-8")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


class Process:
    def __init__(self, case: unittest.TestCase, workdir: Path, userdata: Path, hardware: Path, extra_env=None):
        self.case = case
        self.userdata = userdata
        self.main_port = free_port()
        self.peer_port = free_port()
        self.log_path = workdir / f"{userdata.name}-{time.monotonic_ns()}.log"
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(workdir),
            "USERDATA_ROOT": str(userdata),
            "LOG_DIR": str(workdir / f"logs-{userdata.name}"),
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
            **(extra_env or {}),
        }
        self.log = open(self.log_path, "wb")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "app.main"],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )

    def wait_healthy(self) -> "Process":
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
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw or b"{}")
            except ValueError:
                return error.code, {"raw": raw.decode("utf-8", "replace")}

    def login(self) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.main_port}/v1/api/auth/login",
            data=json.dumps({"username": "admin", "password": "changeme"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return {"Cookie": response.headers["Set-Cookie"].split(";")[0]}

    def info(self) -> dict:
        status, payload = self.request("GET", "/v1/api/peer/info")
        assert status == 200, (status, payload)
        return payload

    def pairing_code(self) -> str:
        status, payload = self.request(
            "POST", "/v1/api/admin/local-network/pairing-code/rotate", {}, headers=self.login()
        )
        assert status == 200, (status, payload)
        return payload["pairing"]["code"]

    def pair_with(self, other: "Process"):
        return self.request(
            "POST",
            "/v1/api/admin/local-network/pair-by-address",
            {"address": f"http://127.0.0.1:{other.main_port}", "pairing_code": other.pairing_code(),
             "certificate_fingerprint": other.info()["certificate_fingerprint"]},
            headers=self.login(),
        )


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class CloneSwarmJoinUAT(unittest.TestCase):
    def setUp(self) -> None:
        from app.storage.state_store import database_path, load_payload, save_payload

        self.database_path, self.load_payload, self.save_payload = database_path, load_payload, save_payload
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-160-swarm-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.running = []
        self.addCleanup(lambda: [process.stop() for process in self.running])
        self.hardware = {name: write_hardware(self.tmp / f"hw-{name}", *ids) for name, ids in HARDWARE.items()}

    def start(self, userdata: Path, machine: str, extra_env=None) -> Process:
        process = Process(self, self.tmp, userdata, self.hardware[machine], extra_env)
        self.running.append(process)
        return process.wait_healthy()

    def peers(self, userdata: Path) -> dict:
        peers = self.load_payload(self.database_path(userdata), "local_paired_peers", {})
        return peers if isinstance(peers, dict) else {}

    def test_clones_join_the_original_swarm_through_normal_pairing(self) -> None:
        original_disk = self.tmp / "original"
        original_disk.mkdir()
        first = self.start(original_disk, "original")
        first.stop()
        self.running.remove(first)
        original_fp = disk_fingerprint(original_disk)

        # The original already belongs to a swarm with a friend.
        db = self.database_path(original_disk)
        friend = {"drone_id": FRIEND_ID, "device_id": FRIEND_ID, "name": "friend", "paired": True,
                  "certificate_fingerprint": FRIEND_FINGERPRINT}
        self.save_payload(db, "local_paired_peers", {FRIEND_ID: friend})
        pins = original_disk / "system" / "drone-app" / "local-peer-certs"
        pins.mkdir(parents=True, exist_ok=True)
        (pins / "friend.crt").write_text("-----BEGIN CERTIFICATE-----\nfriend\n", encoding="utf-8")

        disks = {}
        for name in ("clone-1", "clone-2"):
            disks[name] = self.tmp / name
            shutil.copytree(original_disk, disks[name], symlinks=True)

        original = self.start(original_disk, "original")
        clones = {name: self.start(disks[name], name) for name in disks}

        # Each machine serves its own identity; nothing is inherited.
        infos = {"original": original.info(), **{name: clone.info() for name, clone in clones.items()}}
        self.assertEqual(len({info["drone_id"] for info in infos.values()}), 3)
        self.assertEqual(len({info["certificate_fingerprint"] for info in infos.values()}), 3)
        self.assertEqual(infos["original"]["certificate_fingerprint"], original_fp)
        for name, clone in clones.items():
            self.assertEqual(infos[name]["certificate_fingerprint"], disk_fingerprint(disks[name]))
            self.assertNotEqual(disk_fingerprint(disks[name]), original_fp)
            self.assertEqual(self.peers(disks[name]), {}, msg=f"{name} inherited peers")
            self.assertEqual(list((disks[name] / "system/drone-app/local-peer-certs").glob("*")), [])
            self.assertIn("Clone provisioning completed", clone.text())

        # Normal pairing: each clone dials the original with its current pairing code.
        for name, clone in clones.items():
            status, payload = clone.pair_with(original)
            self.assertEqual(status, 200, msg=f"{name}: {payload}\n{original.text()[-1500:]}")
            clone_peers = self.peers(disks[name])
            self.assertEqual(set(clone_peers), {infos["original"]["drone_id"]})
            self.assertEqual(clone_peers[infos["original"]["drone_id"]]["certificate_fingerprint"], original_fp)

        original_peers = self.peers(original_disk)
        self.assertEqual(
            set(original_peers),
            {FRIEND_ID, infos["clone-1"]["drone_id"], infos["clone-2"]["drone_id"]},
        )
        # The original pinned the clones' NEW certificates, and its friend is untouched.
        self.assertEqual(original_peers[infos["clone-1"]["drone_id"]]["certificate_fingerprint"], infos["clone-1"]["certificate_fingerprint"])
        self.assertEqual(original_peers[infos["clone-2"]["drone_id"]]["certificate_fingerprint"], infos["clone-2"]["certificate_fingerprint"])
        self.assertEqual(original_peers[FRIEND_ID]["certificate_fingerprint"], FRIEND_FINGERPRINT)
        self.assertTrue((pins / "friend.crt").exists())
        self.assertEqual(disk_fingerprint(original_disk), original_fp)

        # Nothing anywhere records a removal of anyone: a clone is a new device, not a replacement.
        for userdata in (original_disk, *disks.values()):
            membership = self.load_payload(self.database_path(userdata), "swarm_membership", {}) or {}
            for peer_id, record in (membership.get("records") or {}).items():
                self.assertEqual(record.get("op"), "add", msg=f"{userdata.name}: {peer_id} {record}")
        for process in (original, *clones.values()):
            self.assertNotIn("forget", process.text().lower().replace("forgotten", ""))

        # A copy that kept the original's certificate is refused and nothing changes.
        before = json.dumps(self.peers(original_disk), sort_keys=True)
        own_pem = (original_disk / "system/drone-app/certs/drone.crt").read_text(encoding="utf-8")
        status, payload = original.request(
            "POST",
            "/v1/api/peer/pair",
            {
                "drone_id": "ca:fe:ca:fe:00:09",
                "name": "unreprovisioned-copy",
                "certificate_pem": own_pem,
                "certificate_fingerprint": original_fp,
                "pairing_code": original.pairing_code(),
            },
            port=original.peer_port,
        )
        self.assertEqual(status, 409, msg=str(payload))
        self.assertEqual(json.dumps(self.peers(original_disk), sort_keys=True), before)
        self.assertEqual(disk_fingerprint(original_disk), original_fp)
        self.assertIn("Duplicate identity observed", original.text())

    def test_environment_pinned_device_id_copied_with_the_image_does_not_survive_cloning(self) -> None:
        """A service environment that pins DRONE_DEVICE_ID travels with the image too."""
        pin = {"DRONE_DEVICE_ID": "pinned-drone-1"}
        original_disk = self.tmp / "original"
        original_disk.mkdir()
        original = self.start(original_disk, "original", pin)
        self.assertEqual(original.info()["drone_id"], "pinned-drone-1")
        original.stop()
        self.running.remove(original)
        clone_disk = self.tmp / "clone-1"
        shutil.copytree(original_disk, clone_disk, symlinks=True)

        clone = self.start(clone_disk, "clone-1", pin)
        clone_id = clone.info()["drone_id"]
        self.assertNotEqual(clone_id, "pinned-drone-1")
        status, health = clone.request("GET", "/health")
        self.assertEqual(health["drone_id"], clone_id)
        self.assertNotEqual(disk_fingerprint(clone_disk), disk_fingerprint(original_disk))
        clone.stop()
        self.running.remove(clone)

        # The next boot keeps the clone's id even though the copied environment still names the old one.
        again = self.start(clone_disk, "clone-1", pin)
        self.assertEqual(again.info()["drone_id"], clone_id)
        self.assertNotIn("Clone", again.text())

    def test_planned_motherboard_swap_keeps_identity_once_through_the_real_api(self) -> None:
        disk = self.tmp / "original"
        disk.mkdir()
        first = self.start(disk, "original")
        original_id, original_fp = first.info()["drone_id"], disk_fingerprint(disk)
        self.save_payload(self.database_path(disk), "local_paired_peers", {FRIEND_ID: {
            "drone_id": FRIEND_ID, "device_id": FRIEND_ID, "name": "friend", "paired": True,
            "certificate_fingerprint": FRIEND_FINGERPRINT}})

        # Authorization needs the administrator's password and a real session.
        path = "/v1/api/admin/identity/authorize-hardware-change"
        self.assertEqual(first.request("POST", path, {"password": "changeme"})[0], 401)
        self.assertEqual(first.request("POST", path, {"password": "wrong"}, headers=first.login())[0], 403)
        self.assertEqual(first.request("POST", path, {"hours": 4}, headers=first.login())[0], 403)
        self.assertIsNone(first.request("GET", "/v1/api/admin/identity/status", headers=first.login())[1]["hardware_change_authorization"])
        status, payload = first.request("POST", path, {"password": "changeme", "hours": 4}, headers=first.login())
        self.assertEqual(status, 200, msg=str(payload))
        first.stop()
        self.running.remove(first)

        # Disk moved to a replacement board: same identity, swarm kept, authorization consumed.
        moved = self.start(disk, "clone-1")
        self.assertEqual(moved.info()["drone_id"], original_id)
        self.assertEqual(disk_fingerprint(disk), original_fp)
        self.assertEqual(set(self.peers(disk)), {FRIEND_ID})
        status, payload = moved.request("GET", "/v1/api/admin/identity/status", headers=moved.login())
        self.assertEqual(payload["status"], "identity_verified")
        self.assertIsNone(payload["hardware_change_authorization"])
        moved.stop()
        self.running.remove(moved)

        # The authorization was single-use: an image of that disk on another machine is a clone.
        image = self.tmp / "clone-2"
        shutil.copytree(disk, image, symlinks=True)
        clone = self.start(image, "clone-2")
        self.assertNotEqual(clone.info()["drone_id"], original_id)
        self.assertNotEqual(disk_fingerprint(image), original_fp)
        self.assertEqual(self.peers(image), {})

        # And the replaced board is now the baseline: the disk reboots there without any change.
        again = self.start(disk, "clone-1")
        self.assertEqual(again.info()["drone_id"], original_id)
        self.assertNotIn("Clone", again.text())


if __name__ == "__main__":
    unittest.main()
