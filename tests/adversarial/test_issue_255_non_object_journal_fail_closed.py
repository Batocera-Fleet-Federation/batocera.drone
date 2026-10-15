"""Issue #255 adversarial: non-object journals fail closed across malformed-input shapes.

These are guard tests. They should pass on the current patch. They probe the
boundaries the #255 UAT suites do not reach:

- whitespace- and newline-padded non-object JSON (hand-edited journal files);
- undecodable bytes that are not valid UTF-8 (a UnicodeDecodeError is not an OSError);
- a non-object journal on a first boot must not persist a logical device id;
- a non-object journal next to an existing certificate with no binding (migration
  state) must hold, not replace the certificate.

Each case asserts the same fail-closed outcome: no certificate minted or replaced,
no logical id adopted from the journal, swarm networking held.
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


RESUMED_ID = "02:11:22:33:44:55"

PADDED_NON_OBJECT_JOURNALS = (
    b"  []  \n",
    b"\n\"%s\"\n" % RESUMED_ID.encode("ascii"),
    b"\tnull\r\n",
)
UNDECODABLE_JOURNALS = (
    b"\xff[]",
    b"[]\x80",
)


def hardware(root: Path) -> HardwareIdentityProvider:
    for name, value in (("product_uuid", "3f713284-3270-4c5f-990c-17231045b68e"),
                        ("board_serial", "BOARD-255-FAIL-CLOSED")):
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


class NonObjectJournalFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-255-fail-closed-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.provider = hardware(self.root / "hardware")

    def _write_raw(self, drone: Settings, raw: bytes) -> Path:
        path = identity._journal_path(drone)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def _assert_held_without_minting(self, drone: Settings, result, journal: Path, raw: bytes) -> None:
        self.assertEqual(result.state, identity.State.RECOVERY_REQUIRED)
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertNotEqual(result.settings.device_id, RESUMED_ID)
        self.assertFalse(drone.drone_cert_file.exists())
        self.assertFalse(drone.drone_key_file.exists())
        # The baseline logical id may be persisted on hold; the journal's id must never be.
        persisted = identity._device_id_path(Path(drone.userdata_root))
        if persisted.exists():
            self.assertEqual(persisted.read_text(encoding="utf-8").strip(), result.settings.device_id)
        self.assertEqual(journal.read_bytes(), raw)

    def test_padded_non_object_journal_on_first_boot_is_held(self) -> None:
        for index, raw in enumerate(PADDED_NON_OBJECT_JOURNALS):
            with self.subTest(journal=raw):
                drone = settings(self.root / f"padded-{index}")
                journal = self._write_raw(drone, raw)
                identity.reset_runtime_state_for_tests()
                result = identity.ensure_identity(drone, self.provider)
                self._assert_held_without_minting(drone, result, journal, raw)

    def test_undecodable_non_object_journal_on_first_boot_is_held(self) -> None:
        for index, raw in enumerate(UNDECODABLE_JOURNALS):
            with self.subTest(journal=raw):
                drone = settings(self.root / f"undecodable-{index}")
                journal = self._write_raw(drone, raw)
                identity.reset_runtime_state_for_tests()
                result = identity.ensure_identity(drone, self.provider)
                self._assert_held_without_minting(drone, result, journal, raw)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required")
    def test_non_object_journal_with_certificate_but_no_binding_does_not_replace_it(self) -> None:
        drone = settings(self.root / "migration")
        first = identity.ensure_identity(drone, self.provider)
        self.assertEqual(first.state, identity.State.READY)
        certificate = drone.drone_cert_file.read_bytes()
        key = drone.drone_key_file.read_bytes()
        device_id = drone.device_id
        identity._binding_path(drone).unlink()

        raw = b"[]"
        journal = self._write_raw(drone, raw)
        identity.reset_runtime_state_for_tests()
        result = identity.ensure_identity(drone, self.provider)

        self.assertEqual(result.state, identity.State.RECOVERY_REQUIRED)
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertEqual(drone.drone_cert_file.read_bytes(), certificate)
        self.assertEqual(drone.drone_key_file.read_bytes(), key)
        self.assertEqual(result.settings.device_id, device_id)
        self.assertEqual(journal.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
