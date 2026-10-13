"""Issue #160: power loss at every provisioning stage must never resurrect the inherited identity.

Requirement (section 9): a failed or interrupted reprovisioning must not reactivate the
cloned identity, no partially provisioned identity may join the swarm, and a retry must
finish the job. Earlier tests inject *exceptions*; real power loss is a hard process
death that runs no exception handler. Each stage here is killed with ``os._exit`` in a
child process, then the disk is booted again with the same (new) hardware.
"""

import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.storage.state_store import database_path, load_payload, save_payload

REPO_ROOT = Path(__file__).resolve().parents[2]

# (module attribute to wrap, "pre"/"post": die before or after the wrapped call returns)
CRASH_POINTS = (
    ("identity._retire_old_identity", "post"),
    ("swarm_reset.reset_swarm_identity_state", "post"),
    ("identity._choose_device_id", "post"),
    ("identity._reset_tailnet", "post"),
    ("DroneCertificateManager._generate_local_certificate", "pre"),
    ("DroneCertificateManager._generate_local_certificate", "post"),
    ("identity._save_binding", "pre"),
    ("identity._save_binding", "post"),
)

CHILD = r"""
import functools, os, sys
from pathlib import Path
from unittest import mock
from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.transfer import swarm_reset
from app.transfer.drone_tls import DroneCertificateManager

root, hardware, target, when = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
owner_name, _, attr = target.partition(".")
owner = {"identity": identity, "swarm_reset": swarm_reset, "DroneCertificateManager": DroneCertificateManager}[owner_name]
original = getattr(owner, attr)

@functools.wraps(original)
def dying(*args, **kwargs):
    if when == "pre":
        os._exit(77)
    result = original(*args, **kwargs)
    os._exit(77)

setattr(owner, attr, dying)
env = {"USERDATA_ROOT": root, "TLS_SELF_SIGNED_DIR": root + "/system/certs"}
with mock.patch.dict("os.environ", env, clear=True):
    settings = Settings.from_env()
identity.ensure_identity(settings, HardwareIdentityProvider(Path(hardware), container_probe=lambda: False, machine="x86_64"))
os._exit(0)
"""


def settings_for(root: Path) -> Settings:
    env = {"USERDATA_ROOT": str(root), "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs")}
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def provider(root: Path) -> HardwareIdentityProvider:
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def write_hardware(root: Path, uuid: str, serial: str) -> Path:
    directory = root / "sys" / "class" / "dmi" / "id"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "product_uuid").write_text(uuid + "\n", encoding="utf-8")
    (directory / "board_serial").write_text(serial + "\n", encoding="utf-8")
    return root


def fingerprint(settings: Settings):
    try:
        pem = settings.drone_cert_file.read_text(encoding="utf-8")
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    except (OSError, ValueError):
        return None


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class PowerLossDuringCloneProvisioning(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        temp = tempfile.TemporaryDirectory(prefix="issue-160-power-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.hw_a = write_hardware(self.tmp / "hw-a", "9a1c4e57-2b0d-4f38-8c61-71d5e3a0b9f2", "ORIGBOARD-0001")
        self.hw_b = write_hardware(self.tmp / "hw-b", "5d7e0c13-8a94-4b26-9f10-c3e8a2d64b75", "CLONEBOARD-0002")
        self.original = settings_for(self.tmp / "original")
        self.assertEqual(identity.ensure_identity(self.original, provider(self.hw_a)).state, identity.State.READY)
        self.original_fingerprint = fingerprint(self.original)
        self.peer_id = "11:22:33:44:55:66"
        save_payload(
            database_path(self.original.userdata_root),
            "local_paired_peers",
            {self.peer_id: {"drone_id": self.peer_id, "certificate_fingerprint": "ab" * 32, "paired": True}},
        )
        pins = self.original.userdata_root / "system" / "drone-app" / "local-peer-certs"
        pins.mkdir(parents=True, exist_ok=True)
        (pins / "peer.crt").write_text("-----BEGIN CERTIFICATE-----\npinned\n", encoding="utf-8")

    def crash_clone(self, target: str, when: str) -> Path:
        root = self.tmp / f"clone-{target.replace('.', '-')}-{when}"
        shutil.copytree(self.original.userdata_root, root)
        completed = subprocess.run(
            [sys.executable, "-c", CHILD, str(root), str(self.hw_b), target, when],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(completed.returncode, 77, msg=f"{target}/{when} did not reach its crash point:\n{completed.stderr[-800:]}")
        return root

    def assert_not_inherited(self, clone: Settings, label: str) -> None:
        # Whatever state the dead process left behind, the inherited key must not be usable.
        if clone.drone_cert_file.is_file() and clone.drone_key_file.is_file():
            self.assertNotEqual(fingerprint(clone), self.original_fingerprint, msg=f"{label}: inherited certificate still present")

    def test_hard_kill_at_every_stage_never_leaves_a_usable_inherited_identity_and_retry_completes(self) -> None:
        for target, when in CRASH_POINTS:
            label = f"{target}:{when}"
            with self.subTest(crash=label):
                identity.reset_runtime_state_for_tests()
                root = self.crash_clone(target, when)
                clone = settings_for(root)
                before_retry_journal = identity._journal_path(clone).exists()
                self.assertTrue(before_retry_journal, msg=f"{label}: no durable journal to resume from")
                if target in {"identity._retire_old_identity"} or target.startswith(("swarm_reset", "identity._choose", "identity._reset", "DroneCert", "identity._save")):
                    self.assert_not_inherited(clone, label)

                identity.reset_runtime_state_for_tests()
                retry = identity.ensure_identity(settings_for(root), provider(self.hw_b))
                self.assertEqual(retry.state, identity.State.READY, msg=f"{label}: retry did not complete ({retry.reason})")
                self.assertEqual(retry.status, identity.STATUS_CLONE, msg=label)
                self.assertTrue(identity.swarm_networking_allowed(), msg=label)
                settled = settings_for(root)
                self.assertNotEqual(fingerprint(settled), self.original_fingerprint, msg=label)
                self.assertNotEqual(retry.settings.device_id, self.original.device_id, msg=label)
                binding = json.loads(identity._binding_path(settled).read_text(encoding="utf-8"))
                self.assertEqual(binding["identity_certificate_fingerprint"], fingerprint(settled), msg=label)
                self.assertEqual(binding["device_id"], retry.settings.device_id, msg=label)
                self.assertEqual(load_payload(database_path(root), "local_paired_peers", "gone"), "gone", msg=label)
                self.assertEqual(list((root / "system" / "drone-app" / "local-peer-certs").glob("*")), [], msg=label)
                self.assertFalse(identity._journal_path(settled).exists(), msg=label)
                self.assertFalse(identity._recovery_path(settled).exists(), msg=label)
                self.assertEqual(settled.drone_key_file.stat().st_mode & 0o777, 0o600, msg=label)
                # And it is stable: the next boot verifies, it does not provision a third identity.
                identity.reset_runtime_state_for_tests()
                again = identity.ensure_identity(settings_for(root), provider(self.hw_b))
                self.assertEqual(again.status, identity.STATUS_VERIFIED, msg=label)
                self.assertEqual(fingerprint(settings_for(root)), fingerprint(settled), msg=label)

    def test_original_is_untouched_by_every_crashed_clone(self) -> None:
        before = self.original.drone_cert_file.read_bytes()
        key = self.original.drone_key_file.read_bytes()
        for target, when in CRASH_POINTS[:3]:
            self.crash_clone(target, when)
        self.assertEqual(self.original.drone_cert_file.read_bytes(), before)
        self.assertEqual(self.original.drone_key_file.read_bytes(), key)
        self.assertIn(self.peer_id, load_payload(database_path(self.original.userdata_root), "local_paired_peers", {}))


if __name__ == "__main__":
    unittest.main()
