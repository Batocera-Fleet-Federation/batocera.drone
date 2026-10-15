"""Issue #255 UAT: a non-object journal.json fails closed through ensure_identity.

The owner decision for #255: a journal that decodes to anything other than a
JSON object is refused, never treated as a missing journal, and never used to
mint a fresh id. The run ends in RECOVERY_REQUIRED with swarm networking held,
and the next start retries from the same journal.

This is the production path (identity/journal.json via _read_json), not a
direct _choose_device_id call. A missing journal on first boot still
provisions; that positive control must keep working so a blanket recovery
cannot make the refusal tests pass.

On an already-bound original, a non-object journal must not mint a replacement
id or replace the certificate. #160 already allows that original to stay READY
after discarding an unreadable journal; this suite only pins 'do not mint'.

Expected, derived from docs/drone-identity.md before relying on the patch:

- First boot with journal.json as array/null/bool/number/string: RECOVERY_REQUIRED,
  swarm_networking_allowed False, tailnet_start_permitted False, no identity
  certificate, device-id unchanged, journal file left in place for retry.
- A second start with that journal still holds and still does not mint a cert.
- A JSON string that happens to be a MAC is not adopted as the logical id.
- Removing the junk journal lets a first boot provision (openssl required).
- A verified original plus a non-object journal does not change cert, key, or
  device-id (never a clone mint on the original hardware).
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
MAC_STRING_JSON = '"%s"' % RESUMED_ID

NON_OBJECT_JOURNALS = (
    "[]",
    '[{"new_device_id": "%s", "reason": "clone"}]' % RESUMED_ID,
    MAC_STRING_JSON,
    "null",
    "true",
    "false",
    "0",
    "7",
    '""',
)


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


class EnsureIdentityNonObjectJournalUat(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-255-journal-type-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.provider = hardware(
            self.root / "hardware",
            "3f713284-3270-4c5f-990c-17231045b68e",
            "BOARD-255-JOURNAL-TYPE",
        )

    def _boot(self, userdata: Path):
        identity.reset_runtime_state_for_tests()
        return identity.ensure_identity(settings(userdata), self.provider)

    def _write_journal(self, drone: Settings, text: str) -> Path:
        path = identity._journal_path(drone)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_first_boot_non_object_journal_holds_and_does_not_mint(self) -> None:
        for payload in NON_OBJECT_JOURNALS:
            with self.subTest(journal=payload):
                userdata = self.root / ("first-" + str(abs(hash(payload))))
                drone = settings(userdata)
                before_id = drone.device_id
                journal_path = self._write_journal(drone, payload)
                self.assertIs(
                    identity._read_json(journal_path),
                    identity._CORRUPT,
                    msg="a non-object journal must not be treated as absent",
                )

                result = identity.ensure_identity(drone, self.provider)

                self.assertEqual(result.state, identity.State.RECOVERY_REQUIRED)
                self.assertEqual(result.reason, "journal_unreadable")
                self.assertFalse(identity.swarm_networking_allowed())
                self.assertFalse(identity.tailnet_start_permitted(drone, self.provider))
                self.assertFalse(drone.drone_cert_file.exists())
                self.assertFalse(drone.drone_key_file.exists())
                self.assertEqual(result.settings.device_id, before_id)
                self.assertNotEqual(result.settings.device_id, RESUMED_ID)
                self.assertEqual(journal_path.read_text(encoding="utf-8"), payload)
                self.assertFalse(identity._binding_path(drone).exists())

                retry = self._boot(userdata)
                self.assertEqual(retry.state, identity.State.RECOVERY_REQUIRED)
                self.assertEqual(retry.reason, "journal_unreadable")
                self.assertFalse(identity.swarm_networking_allowed())
                self.assertFalse(drone.drone_cert_file.exists())
                self.assertEqual(journal_path.read_text(encoding="utf-8"), payload)

    def test_json_string_mac_journal_is_not_adopted_as_the_logical_id(self) -> None:
        userdata = self.root / "mac-string"
        drone = settings(userdata)
        self._write_journal(drone, MAC_STRING_JSON)
        result = identity.ensure_identity(drone, self.provider)
        self.assertEqual(result.state, identity.State.RECOVERY_REQUIRED)
        self.assertNotEqual(result.settings.device_id, RESUMED_ID)
        self.assertNotEqual(drone.device_id, RESUMED_ID)
        persisted = identity._device_id_path(Path(drone.userdata_root))
        if persisted.exists():
            self.assertNotEqual(persisted.read_text(encoding="utf-8").strip(), RESUMED_ID)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required")
    def test_missing_journal_on_the_same_tree_still_provisions_after_junk_is_removed(self) -> None:
        userdata = self.root / "positive-missing"
        drone = settings(userdata)
        journal_path = self._write_journal(drone, "[]")
        held = identity.ensure_identity(drone, self.provider)
        self.assertEqual(held.state, identity.State.RECOVERY_REQUIRED)
        self.assertFalse(drone.drone_cert_file.exists())

        journal_path.unlink()
        identity.reset_runtime_state_for_tests()
        ready = identity.ensure_identity(settings(userdata), self.provider)
        self.assertEqual(ready.state, identity.State.READY)
        self.assertTrue(identity.swarm_networking_allowed())
        self.assertTrue(Path(ready.settings.drone_cert_file).exists())

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required")
    def test_bound_original_does_not_mint_a_new_id_from_a_non_object_journal(self) -> None:
        userdata = self.root / "original"
        original = settings(userdata)
        first = identity.ensure_identity(original, self.provider)
        self.assertEqual(first.state, identity.State.READY)
        cert = original.drone_cert_file.read_bytes()
        key = original.drone_key_file.read_bytes()
        device_id = original.device_id
        binding = identity._binding_path(original).read_bytes()

        for payload in ("[]", "null", MAC_STRING_JSON, "true"):
            with self.subTest(journal=payload):
                self._write_journal(original, payload)
                result = self._boot(userdata)
                self.assertNotEqual(result.status, identity.STATUS_CLONE)
                self.assertEqual(original.drone_cert_file.read_bytes(), cert)
                self.assertEqual(original.drone_key_file.read_bytes(), key)
                self.assertEqual(result.settings.device_id, device_id)
                self.assertEqual(identity._binding_path(original).read_bytes(), binding)
                self.assertNotEqual(result.settings.device_id, RESUMED_ID)


if __name__ == "__main__":
    unittest.main()
