"""Issue #255 UAT: corrupt journal does not authorize identity repair.

A non-object journal is untrusted provisioning intent.  An intact binding and
matching hardware are not enough to discard it: the existing certificate/key
pair must also be valid.  Otherwise an attacker or torn write could combine a
bad journal with a missing key and silently mint a replacement identity.

Expected from the journal-type contract before inspecting implementation:

* a JSON array is not a missing journal;
* a bound original whose key is absent enters recovery, keeps networking held,
  and leaves both the journal and surviving certificate untouched;
* no fresh key/certificate or logical device id is minted while that evidence
  is incomplete.
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


def _hardware(root: Path) -> HardwareIdentityProvider:
    for name, value in (("product_uuid", "55d1cdbf-5c53-4a0d-a255-0ce7da3775be"),
                        ("board_serial", "BOARD-255-INVALID-PAIR")):
        path = root / "sys/class/dmi/id" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def _settings(root: Path) -> Settings:
    with mock.patch.dict(os.environ, {
        "USERDATA_ROOT": str(root),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
    }, clear=True):
        return Settings.from_env()


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class CorruptJournalInvalidPairUatTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-255-invalid-pair-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.provider = _hardware(self.root / "hardware")

    def test_non_object_journal_with_missing_key_holds_instead_of_repairing(self) -> None:
        drone = _settings(self.root / "userdata")
        first = identity.ensure_identity(drone, self.provider)
        self.assertEqual(first.state, identity.State.READY)
        certificate = drone.drone_cert_file.read_bytes()
        binding = identity._binding_path(drone).read_bytes()
        device_id = drone.device_id

        drone.drone_key_file.unlink()
        journal = identity._journal_path(drone)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text("[]", encoding="utf-8")
        identity.reset_runtime_state_for_tests()

        result = identity.ensure_identity(_settings(self.root / "userdata"), self.provider)

        self.assertEqual(result.state, identity.State.RECOVERY_REQUIRED)
        self.assertEqual(result.reason, "journal_unreadable")
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertEqual(result.settings.device_id, device_id)
        self.assertEqual(drone.drone_cert_file.read_bytes(), certificate)
        self.assertFalse(drone.drone_key_file.exists(), "recovery must not mint a replacement key")
        self.assertEqual(identity._binding_path(drone).read_bytes(), binding)
        self.assertEqual(journal.read_text(encoding="utf-8"), "[]")


if __name__ == "__main__":
    unittest.main()
