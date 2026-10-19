"""Issue #252 UAT: a MagicMock *value* in a real dict journal must not become a device id.

Issue #252 (filed from a #217 probe): with a real userdata_root (system/drone-app
present) and journal={'new_device_id': unittest.mock.MagicMock()},
identity_provisioning._choose_device_id returned str(the mock) — a repr such as
"<MagicMock id='…'>" — instead of refusing the non-string resumed id.

Production journals are dicts from json.loads of identity/journal.json, so a
mock cannot appear as a JSON value. The helper is still called with an in-memory
dict during clone provisioning, and it must not stringify an invalid resumed
value into the logical Drone id. _valid_journal already rejects a non-str
new_device_id on the ensure_identity resume path; this helper must match that
contract.

Expected behavior, derived from the issue and docs/drone-identity.md before
relying on the patch:

- A real dict whose new_device_id is a unittest mock raises IdentityError. It
  must not return a mock repr, must not persist one under system/drone-app/
  device-id, and must not mint a MAC as if the key were absent.
- json.loads of a journal.json object with a non-string new_device_id (number,
  array, object, true) raises the same way: those are the values a production
  journal can actually carry.
- A well-formed string id still resumes and does not rewrite an existing
  device-id file.
- Falsy JSON null / empty string still mint (the #217 resume-branch contract),
  and the minted id is a locally-administered unicast MAC, not settings.device_id.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:11:22:33:44:55"
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"
INVALID_JOURNALED_ID = "the journaled device id is not a valid Drone id"


def _mock_named_entries(directory: Path) -> list[str]:
    names: list[str] = []
    if not directory.exists():
        return names
    for path in directory.rglob("*"):
        name = path.name
        if "MagicMock" in name or "NonCallable" in name or name.startswith("<Mock"):
            names.append(str(path.relative_to(directory)))
    return names


class _ChooseDeviceIdSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmpdir.name).resolve()
        os.chdir(self.cwd)
        self.userdata = self.cwd / "userdata"
        (self.userdata / "system" / "drone-app").mkdir(parents=True)
        self.persisted = self.userdata / "system" / "drone-app" / "device-id"
        self.settings = SimpleNamespace(userdata_root=self.userdata, device_id=CONFIGURED_ID)

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._tmpdir.cleanup()
        self._env.stop()

    def _choose(self, journal):
        return identity._choose_device_id(self.settings, journal)

    def _assert_no_mock_artifacts(self) -> None:
        self.assertEqual(_mock_named_entries(self.cwd), [])
        if self.persisted.exists():
            text = self.persisted.read_text(encoding="utf-8")
            self.assertNotIn("MagicMock", text)
            self.assertNotIn("NonCallable", text)
            self.assertNotIn("<Mock", text)


class Issue252MagicMockValueReproductionTests(_ChooseDeviceIdSandbox):
    """The #252 probe: real userdata_root + dict journal + MagicMock value."""

    def test_magicmock_new_device_id_raises_instead_of_returning_mock_repr(self) -> None:
        with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
            self._choose({"new_device_id": mock.MagicMock()})
        self.assertFalse(self.persisted.exists())
        self._assert_no_mock_artifacts()

    def test_swallowed_identity_error_still_does_not_yield_a_mock_repr_device_id(self) -> None:
        chosen = None
        try:
            chosen = self._choose({"new_device_id": mock.MagicMock()})
        except identity.IdentityError:
            chosen = None
        self.assertIsNone(chosen)
        self.assertFalse(self.persisted.exists())

    def test_magicmock_value_does_not_overwrite_an_existing_persisted_id(self) -> None:
        self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        with self.assertRaises(identity.IdentityError):
            self._choose({"new_device_id": mock.MagicMock()})
        self.assertEqual(self.persisted.read_text(encoding="utf-8"), CONFIGURED_ID + "\n")
        self._assert_no_mock_artifacts()

    def test_mock_configured_to_stringify_as_a_mac_is_still_rejected(self) -> None:
        value = mock.MagicMock()
        value.__str__ = mock.Mock(return_value=RESUMED_ID)
        value.__repr__ = mock.Mock(return_value=RESUMED_ID)
        with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
            self._choose({"new_device_id": value})
        self.assertFalse(self.persisted.exists())

    def test_rejection_does_not_poison_a_later_valid_resume(self) -> None:
        with self.assertRaises(identity.IdentityError):
            self._choose({"new_device_id": mock.MagicMock()})
        chosen = self._choose({"new_device_id": RESUMED_ID})
        self.assertEqual(chosen, RESUMED_ID)
        self.assertFalse(self.persisted.exists())


class Issue252JsonJournalValueTests(_ChooseDeviceIdSandbox):
    """Values json.loads can put in a production journal dict."""

    def _roundtrip(self, new_device_id):
        payload = {"new_device_id": new_device_id}
        path = self.userdata / "system" / "drone-app" / "identity" / "journal.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_json_number_array_object_and_true_are_rejected(self) -> None:
        for label, raw in (
            ("int", 12345),
            ("float", 1.0),
            ("true", True),
            ("array", [RESUMED_ID]),
            ("object", {"id": RESUMED_ID}),
            ("mock-looking string", "<MagicMock id='1'>"),
            ("spaces", "bad id with spaces!"),
        ):
            with self.subTest(value=label):
                if self.persisted.exists():
                    self.persisted.unlink()
                journal = self._roundtrip(raw)
                self.assertIsInstance(journal, dict)
                with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
                    self._choose(journal)
                self.assertFalse(self.persisted.exists())
                self._assert_no_mock_artifacts()

    def test_json_string_mac_still_resumes(self) -> None:
        journal = self._roundtrip(RESUMED_ID)
        chosen = self._choose(journal)
        self.assertEqual(chosen, RESUMED_ID)
        self.assertFalse(self.persisted.exists())


class Issue252RealJournalPositiveControlTests(_ChooseDeviceIdSandbox):
    """A blanket raise on every journal would make the rejection tests pass vacuously."""

    def test_empty_dict_mints_a_fresh_id_not_the_configured_one(self) -> None:
        chosen = self._choose({})
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertNotIn("MagicMock", chosen)
        self.assertEqual(self.persisted.read_text(encoding="utf-8").strip(), chosen)
        first_octet = int(chosen.split(":", 1)[0], 16)
        self.assertEqual(first_octet & 0x02, 0x02, msg="fresh id must be locally administered")
        self.assertEqual(first_octet & 0x01, 0, msg="fresh id must be unicast")

    def test_dict_with_new_device_id_resumes_that_id_and_does_not_rewrite_the_file(self) -> None:
        self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        chosen = self._choose({"new_device_id": RESUMED_ID})
        self.assertEqual(chosen, RESUMED_ID)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertEqual(self.persisted.read_text(encoding="utf-8"), CONFIGURED_ID + "\n")

    def test_padded_json_string_resumes_the_normalized_id(self) -> None:
        chosen = self._choose({"new_device_id": "  " + RESUMED_ID + "  \n"})
        self.assertEqual(chosen, RESUMED_ID)
        self.assertFalse(self.persisted.exists())

    def test_only_null_new_device_id_mints_instead_of_resuming(self) -> None:
        chosen = self._choose({"new_device_id": None})
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertNotEqual(chosen, "None")

    def test_present_falsy_malformed_new_device_id_is_rejected_without_minting(self) -> None:
        for journal in ({"new_device_id": ""}, {"new_device_id": 0}):
            with self.subTest(journal=journal):
                if self.persisted.exists():
                    self.persisted.unlink()
                with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
                    self._choose(journal)
                self.assertFalse(self.persisted.exists())


if __name__ == "__main__":
    unittest.main()
