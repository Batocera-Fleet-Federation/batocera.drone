"""An image made during provisioning must not reuse an in-flight identity elsewhere.

Issue #160 requires ordinary full-disk images, repeated cloning, and safe recovery after
interruption. A valid journal is copied by a disk image just like the certificate. Its
stored hardware evidence must be reconciled with the booting machine before a resumed
run may treat the certificate or logical id in that journal as its own.
"""

import hashlib
import json
import os
import shutil
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.storage.state_store import database_path, load_payload, save_payload


def hardware(root: Path, uuid: str, board: str) -> HardwareIdentityProvider:
    dmi = root / "sys/class/dmi/id"
    dmi.mkdir(parents=True)
    (dmi / "product_uuid").write_text(uuid + "\n", encoding="utf-8")
    (dmi / "board_serial").write_text(board + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def settings(root: Path) -> Settings:
    with mock.patch.dict(os.environ, {
        "USERDATA_ROOT": str(root), "TLS_SELF_SIGNED_DIR": str(root / "system/certs")
    }, clear=True):
        return Settings.from_env()


def fingerprint(s: Settings) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(s.drone_cert_file.read_text(encoding="utf-8"))).hexdigest()


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class ImagedJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        tmp = tempfile.TemporaryDirectory(prefix="issue-160-imaged-journal-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.a = hardware(self.tmp / "hw-a", "c25ddfd4-6e36-48cd-b489-1843d4da74ad", "BOARD-ORIGINAL-8291")
        self.b = hardware(self.tmp / "hw-b", "20b5f8b7-5962-4b8e-985a-bc3c6ace160d", "BOARD-CLONE-8172")
        self.c = hardware(self.tmp / "hw-c", "f12be61f-6c32-4ba4-b6bf-5ef769e43d2c", "BOARD-THIRD-6482")

    def test_image_of_interrupted_first_provisioning_gets_fresh_identity(self) -> None:
        original = settings(self.tmp / "original")
        with mock.patch.object(identity, "_save_binding", side_effect=OSError("interrupted write")):
            failed = identity.ensure_identity(original, self.a)
        self.assertEqual(failed.state, identity.State.RECOVERY_REQUIRED)
        self.assertEqual(json.loads(identity._journal_path(original).read_text())["reason"], "initial")
        original_fp = fingerprint(original)
        clone_root = self.tmp / "clone"
        shutil.copytree(original.userdata_root, clone_root)

        identity.reset_runtime_state_for_tests()
        clone_result = identity.ensure_identity(settings(clone_root), self.b)
        self.assertEqual(clone_result.state, identity.State.READY)
        self.assertNotEqual(fingerprint(settings(clone_root)), original_fp,
                            "the clone accepted a certificate created on the source hardware")
        self.assertNotEqual(clone_result.settings.device_id, original.device_id)
        self.assertEqual(fingerprint(original), original_fp, "clone changed the source disk")

        identity.reset_runtime_state_for_tests()
        original_result = identity.ensure_identity(settings(original.userdata_root), self.a)
        self.assertEqual(original_result.state, identity.State.READY)
        self.assertEqual(fingerprint(original), original_fp)

    def test_image_of_interrupted_clone_provisioning_gets_third_identity(self) -> None:
        original = settings(self.tmp / "original")
        self.assertEqual(identity.ensure_identity(original, self.a).state, identity.State.READY)
        peer_id = "aa:bb:cc:dd:ee:01"
        save_payload(database_path(original.userdata_root), "local_paired_peers", {
            peer_id: {"drone_id": peer_id, "certificate_fingerprint": "a" * 64, "paired": True}
        })
        middle_root = self.tmp / "middle"
        shutil.copytree(original.userdata_root, middle_root)
        middle = settings(middle_root)
        with mock.patch.object(identity, "_save_binding", side_effect=OSError("interrupted write")):
            failed = identity.ensure_identity(middle, self.b)
        self.assertEqual(failed.state, identity.State.RECOVERY_REQUIRED)
        journal = json.loads(identity._journal_path(middle).read_text())
        self.assertEqual(journal["reason"], "clone")
        self.assertTrue(journal["steps"].get("certificate"))
        middle_fp = fingerprint(middle)
        middle_id = journal["new_device_id"]
        third_root = self.tmp / "third"
        shutil.copytree(middle_root, third_root)

        identity.reset_runtime_state_for_tests()
        third_result = identity.ensure_identity(settings(third_root), self.c)
        self.assertEqual(third_result.state, identity.State.READY)
        self.assertNotEqual(fingerprint(settings(third_root)), middle_fp,
                            "a copied provisioning journal caused two clones to share a certificate")
        self.assertNotEqual(third_result.settings.device_id, middle_id)
        self.assertEqual(load_payload(database_path(third_root), "local_paired_peers", {}), {})

        identity.reset_runtime_state_for_tests()
        middle_result = identity.ensure_identity(settings(middle_root), self.b)
        self.assertEqual(middle_result.state, identity.State.READY)
        self.assertEqual(fingerprint(settings(middle_root)), middle_fp)
        self.assertEqual(fingerprint(original), json.loads(identity._binding_path(original).read_text())["identity_certificate_fingerprint"])


if __name__ == "__main__":
    unittest.main()
