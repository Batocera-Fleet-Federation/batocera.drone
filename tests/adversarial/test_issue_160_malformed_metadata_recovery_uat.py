"""Issue #160: parseable-but-malformed identity metadata must not trap or destroy a verified original.

Requirement 3/9: for an established installation with insufficient or unusable evidence,
"preserve its existing keys and state ... and provide actionable diagnostics rather than
repeatedly destroying identity". A journal or binding that is valid JSON but structurally
meaningless (empty object, wrong field types) carries no trustworthy intent. On the machine the
binding was made for, the intact identity must survive, and the administrator must have a
way back to a working Drone that keeps it (or startup recovers by itself).
"""

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.storage.state_store import database_path, load_payload, save_payload
from app.transfer.drone_tls import DroneCertificateManager

PEERS = {"peer-1": {"drone_id": "peer-1", "certificate_fingerprint": "a" * 64}}


def hardware(root: Path, uuid: str, serial: str) -> HardwareIdentityProvider:
    for name, value in (("product_uuid", uuid), ("board_serial", serial)):
        path = root / "sys/class/dmi/id" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def settings(root: Path) -> Settings:
    with mock.patch.dict(os.environ, {
        "USERDATA_ROOT": str(root),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
    }, clear=True):
        return Settings.from_env()


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class MalformedMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-160-malformed-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.provider = hardware(self.tmp / "hw", "3f713284-3270-4c5f-990c-17231045b68e", "BOARD-ORIGINAL-8941")
        self.original = settings(self.tmp / "userdata")
        self.assertEqual(identity.ensure_identity(self.original, self.provider).state, identity.State.READY)
        save_payload(database_path(self.original.userdata_root), "local_paired_peers", PEERS)
        self.snapshot = self.capture()

    def capture(self) -> dict:
        return {
            "cert": self.original.drone_cert_file.read_bytes(),
            "key": self.original.drone_key_file.read_bytes(),
            "peers": load_payload(database_path(self.original.userdata_root), "local_paired_peers", {}),
            "device_id": self.original.device_id,
        }

    def assertOriginalIntact(self, label: str) -> None:
        self.assertEqual(self.capture(), self.snapshot, msg=f"{label}: identity or swarm state changed")

    def boot(self):
        identity.reset_runtime_state_for_tests()
        return identity.ensure_identity(settings(self.original.userdata_root), self.provider)

    def assertNotTrapped(self, label: str) -> None:
        """Boot; if held, the administrator's 'keep' must be offered, work, and lead to READY."""
        result = self.boot()
        self.assertOriginalIntact(label)
        if result.state is identity.State.RECOVERY_REQUIRED:
            payload = identity.status_payload(self.original, self.provider)
            self.assertIn(
                "keep", (payload["recovery"] or {}).get("actions", []),
                msg=f"{label}: held as {result.reason!r} with only {payload['recovery']} - the intact original can never be kept",
            )
            identity.resolve_recovery(self.original, "keep", self.provider, actor="admin")
            self.assertOriginalIntact(label)
            result = self.boot()
        self.assertEqual(result.state, identity.State.READY, msg=f"{label}: never reaches READY ({result.reason})")
        self.assertTrue(identity.swarm_networking_allowed())
        self.assertOriginalIntact(label)

    def test_structurally_meaningless_journals_do_not_trap_the_original(self) -> None:
        for payload in (
            "{}",
            '{"steps": "x"}',
            '{"reason": 5, "state": "nope", "steps": []}',
            '{"reason": "clone"}',
        ):
            with self.subTest(journal=payload):
                identity._journal_path(self.original).write_text(payload, encoding="utf-8")
                self.assertNotTrapped(f"journal {payload}")
                identity._journal_path(self.original).unlink(missing_ok=True)
                identity._recovery_path(self.original).unlink(missing_ok=True)

    def test_wrong_typed_binding_fields_do_not_trap_or_destroy_the_original(self) -> None:
        good = json.loads(identity._binding_path(self.original).read_text(encoding="utf-8"))
        for label, mutate in (
            ("device_id list", lambda d: d.update(device_id=[1, 2])),
            ("device_id number", lambda d: d.update(device_id=12345)),
            ("device_id object", lambda d: d.update(device_id={"a": 1})),
        ):
            with self.subTest(binding=label):
                document = copy.deepcopy(good)
                mutate(document)
                identity._binding_path(self.original).write_text(json.dumps(document), encoding="utf-8")
                result = self.boot()
                self.assertOriginalIntact(label)
                # Whatever the verdict, the diagnosis must name the damaged binding, not claim a failed provisioning.
                if result.state is identity.State.RECOVERY_REQUIRED:
                    self.assertIn("binding", result.reason, msg=f"{label}: {result.reason}")
                identity._recovery_path(self.original).unlink(missing_ok=True)

    def test_a_genuinely_interrupted_clone_provisioning_still_resumes(self) -> None:
        """Control: the protection above must not turn real interrupted journals into no-ops."""
        image = self.tmp / "image"
        shutil.copytree(self.original.userdata_root, image, symlinks=True)
        other = hardware(self.tmp / "hw2", "7b2d3e4f-5c60-4b71-9c82-ad93e1f20314", "CLONEBOARD-0002")
        clone = settings(image)
        with mock.patch.object(DroneCertificateManager, "_generate_local_certificate", side_effect=RuntimeError("power loss")):
            held = identity.ensure_identity(clone, other)
        self.assertEqual(held.state, identity.State.RECOVERY_REQUIRED)
        self.assertTrue(identity._journal_path(clone).exists())
        identity.reset_runtime_state_for_tests()
        resumed = identity.ensure_identity(settings(image), other)
        self.assertEqual(resumed.state, identity.State.READY)
        self.assertEqual(resumed.status, identity.STATUS_CLONE)
        self.assertNotEqual(clone.drone_cert_file.read_bytes(), self.snapshot["cert"])
        self.assertEqual(load_payload(database_path(image), "local_paired_peers", {}), {})


if __name__ == "__main__":
    unittest.main()
