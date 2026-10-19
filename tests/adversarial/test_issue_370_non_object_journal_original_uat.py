"""Issue #370 UAT: a verified original with a non-object journal stays READY.

The #370 report expected a bound original whose hardware still matches to be
held offline when identity/journal.json decoded to a JSON array, while torn JSON
stayed READY. Decision (docs/drone-identity.md, "Journal type contract", #366/#369):
any journal that is not a JSON object is treated exactly like a torn journal. The
hardware verdict is re-derived from the durable binding, so an intact original is
discarded from the journal and stays READY, and a confident clone still reprovisions.

These tests pin that outcome end to end with the real file on disk, and assert that
a non-object journal and torn JSON produce the same observable result.
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


NON_OBJECT_JOURNALS = ["[]", "[1, 2]", '"text"', "42", "true", "null"]


def _hardware(root: Path) -> HardwareIdentityProvider:
    values = {
        "product_uuid": "5b0e1d7a-8c2f-4e3b-9a61-2f7c4d8e0b13",
        "board_serial": "BOARD-ORIGINAL-0370",
    }
    for name, value in values.items():
        path = root / "sys/class/dmi/id" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def _settings(userdata: Path) -> Settings:
    with mock.patch.dict(os.environ, {
        "USERDATA_ROOT": str(userdata),
        "TLS_SELF_SIGNED_DIR": str(userdata / "system" / "certs"),
    }, clear=True):
        return Settings.from_env()


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class NonObjectJournalOriginalUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-370-journal-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.provider = _hardware(self.root / "hardware")

    def _verified_original(self, name: str) -> Settings:
        """Provision an original, then return fresh settings for the same userdata."""
        userdata = self.root / name
        drone = _settings(userdata)
        first = identity.ensure_identity(drone, self.provider)
        self.assertEqual(first.state, identity.State.READY)
        identity.reset_runtime_state_for_tests()
        return drone

    def _write_journal(self, drone: Settings, text: str) -> Path:
        journal = identity._journal_path(drone)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(text, encoding="utf-8")
        return journal

    def test_non_object_journal_on_verified_original_stays_ready(self) -> None:
        for index, payload in enumerate(NON_OBJECT_JOURNALS):
            with self.subTest(payload=payload):
                drone = self._verified_original(f"original-{index}")
                certificate = drone.drone_cert_file.read_bytes()
                private_key = drone.drone_key_file.read_bytes()
                binding = identity._binding_path(drone).read_bytes()
                device_id = drone.device_id
                journal = self._write_journal(drone, payload)

                result = identity.ensure_identity(_settings(drone.userdata_root), self.provider)

                self.assertEqual(result.state, identity.State.READY)
                self.assertTrue(identity.swarm_networking_allowed())
                self.assertFalse(journal.exists(), "the verified original discards the unreadable journal")
                self.assertEqual(result.settings.device_id, device_id)
                self.assertEqual(drone.drone_cert_file.read_bytes(), certificate)
                self.assertEqual(drone.drone_key_file.read_bytes(), private_key)
                self.assertEqual(identity._binding_path(drone).read_bytes(), binding)
                identity.reset_runtime_state_for_tests()

    def test_non_object_journal_matches_torn_json_outcome(self) -> None:
        torn = self._verified_original("torn")
        self._write_journal(torn, "{trunc")
        torn_result = identity.ensure_identity(_settings(torn.userdata_root), self.provider)
        identity.reset_runtime_state_for_tests()

        array = self._verified_original("array")
        self._write_journal(array, "[]")
        array_result = identity.ensure_identity(_settings(array.userdata_root), self.provider)

        self.assertEqual(array_result.state, torn_result.state)
        self.assertEqual(array_result.status, torn_result.status)
        self.assertEqual(array_result.reason, torn_result.reason)
        self.assertEqual(identity.swarm_networking_allowed(), True)
        self.assertEqual(identity._journal_path(array).exists(), identity._journal_path(torn).exists())

    def test_non_object_journal_on_confident_clone_still_reprovisions(self) -> None:
        original = self._verified_original("clone-source")
        self._write_journal(original, "[]")
        clone_provider = _hardware(self.root / "clone-hardware")
        (self.root / "clone-hardware" / "sys/class/dmi/id" / "product_uuid").write_text(
            "9d4c2a10-1111-4222-8333-444455556666\n", encoding="utf-8"
        )
        old_device_id = original.device_id

        result = identity.ensure_identity(_settings(original.userdata_root), clone_provider)

        self.assertNotEqual(result.settings.device_id, old_device_id)
        self.assertNotEqual(result.status, identity.STATUS_RECOVERY)


if __name__ == "__main__":
    unittest.main()
