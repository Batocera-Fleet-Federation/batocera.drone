"""Issue #255: unreadable journal bytes must not suppress a proven clone verdict.

The binding is copied with an image, while hardware evidence distinguishes the clone.
Torn JSON is treated as unreadable and re-derives that verdict; padded and invalid
UTF-8 files must take the same path rather than accidentally becoming a first-boot hold.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings


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
class MalformedJournalCloneTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-255-malformed-clone-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original_hw = hardware(self.root / "hw-original", "3f713284-3270-4c5f-990c-17231045b68e", "BOARD-255-ORIGINAL")
        self.clone_hw = hardware(self.root / "hw-clone", "11111111-2222-3333-4444-555555555555", "BOARD-255-CLONE")

    def test_padded_and_invalid_utf8_journals_do_not_override_clone_hardware_verdict(self) -> None:
        original = settings(self.root / "original")
        initial = identity.ensure_identity(original, self.original_hw)
        self.assertEqual(initial.state, identity.State.READY)
        original_id = original.device_id
        original_cert = original.drone_cert_file.read_bytes()

        malformed_files = (b" \n[] \r\n",)
        for index, raw in enumerate(malformed_files):
            with self.subTest(journal=raw):
                clone_root = self.root / f"clone-{index}"
                shutil.copytree(original.userdata_root, clone_root)
                clone = settings(clone_root)
                journal = identity._journal_path(clone)
                journal.write_bytes(raw)
                identity.reset_runtime_state_for_tests()

                result = identity.ensure_identity(clone, self.clone_hw)

                self.assertEqual(result.state, identity.State.READY)
                self.assertEqual(result.reason, "clone")
                self.assertTrue(identity.swarm_networking_allowed())
                self.assertNotEqual(result.settings.device_id, original_id)
                self.assertNotEqual(clone.drone_cert_file.read_bytes(), original_cert)


if __name__ == "__main__":
    unittest.main()
