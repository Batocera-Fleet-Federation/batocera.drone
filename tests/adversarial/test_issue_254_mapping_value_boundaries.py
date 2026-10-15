"""Issue #254 boundaries: real Mapping journals must reject bad values, not coerce them.

Expected behavior, derived from the issue and docs/drone-identity.md *before* the
patch was read:

- A real Mapping (UserDict, MappingProxyType) is a journal, so its resumed
  new_device_id is judged exactly like a dict's: a well-formed string resumes, and
  anything else raises the *id* error, never "must be a mapping".
- Byte strings are not device ids even when they spell a valid MAC. A dict rejects
  them, and so must a UserDict.
- Something that merely *coerces* into a mapping (dict([...]) pairs, a UserString,
  a UserList) is not a Mapping and must be refused with the mapping error, not
  converted into a journal and resumed.
- The rejection leaves the persisted device-id file and the userdata tree alone.
"""

from __future__ import annotations

import collections
import os
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:11:22:33:44:55"
JOURNAL_MUST_BE_MAPPING = "the identity journal must be a mapping"
INVALID_ID = "the journaled device id is not a valid Drone id"


class _StrSubclassId(str):
    """A real str subclass carrying a well-formed id."""


class _Tree(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cwd = Path(self._tmpdir.name).resolve()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, self._previous_cwd)
        self.userdata = self.cwd / "userdata"
        (self.userdata / "system" / "drone-app").mkdir(parents=True)
        self.persisted = self.userdata / "system" / "drone-app" / "device-id"
        self.settings = SimpleNamespace(userdata_root=self.userdata, device_id=CONFIGURED_ID)

    def _tree(self) -> dict:
        entries = {}
        for path in sorted(self.userdata.rglob("*")):
            key = path.relative_to(self.userdata).as_posix()
            entries[key] = path.read_bytes() if path.is_file() else None
        return entries

    def _choose(self, journal):
        """Run one choice; a rejected journal must leave the userdata tree exactly as it was."""
        self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        before = self._tree()
        try:
            outcome = ("returned", identity._choose_device_id(self.settings, journal))
        except identity.IdentityError as error:
            outcome = ("identity-error", str(error))
            self.assertEqual(self._tree(), before, "a rejected journal changed the userdata tree")
        except Exception as error:  # noqa: BLE001 - a leaked type is the finding
            outcome = ("leaked", type(error).__name__, str(error))
        return outcome


class RealMappingValueBoundaryTests(_Tree):
    def _assert_all_real_mappings_agree(self, content, expected):
        """Dict, UserDict and MappingProxyType must reach the same outcome for one value."""
        dict_outcome = self._choose(dict(content))
        self.assertEqual(dict_outcome, expected, "dict baseline changed")
        for label, journal in (
            ("UserDict", collections.UserDict(content)),
            ("MappingProxyType", MappingProxyType(dict(content))),
        ):
            with self.subTest(journal=label):
                self.assertEqual(self._choose(journal), dict_outcome)

    def test_bytes_spelling_a_valid_mac_is_rejected_as_an_invalid_id(self) -> None:
        # Bytes are not a device id even when they look like one; never decoded or resumed.
        self._assert_all_real_mappings_agree(
            {"new_device_id": RESUMED_ID.encode()},
            ("identity-error", INVALID_ID),
        )

    def test_bytearray_spelling_a_valid_mac_is_rejected_as_an_invalid_id(self) -> None:
        self._assert_all_real_mappings_agree(
            {"new_device_id": bytearray(RESUMED_ID, "ascii")},
            ("identity-error", INVALID_ID),
        )

    def test_str_subclass_with_a_valid_id_resumes_the_same_as_dict(self) -> None:
        # Only exact string *content* matters; a str subclass that holds a valid id is judged
        # by its value, identically for dict and for real Mapping journals.
        self._assert_all_real_mappings_agree(
            {"new_device_id": _StrSubclassId(RESUMED_ID)},
            ("returned", RESUMED_ID),
        )

    def test_a_bad_resumed_id_never_falls_back_to_the_configured_pin(self) -> None:
        # A present, non-string id is rejected outright; it must not resume settings.device_id.
        for journal in (collections.UserDict({"new_device_id": 0.5}),
                        MappingProxyType({"new_device_id": 0.5})):
            with self.subTest(journal=type(journal).__name__):
                self.assertEqual(self._choose(journal), ("identity-error", INVALID_ID))

    def test_a_falsy_bytes_id_mints_instead_of_resuming_the_pin(self) -> None:
        # Empty bytes are a falsy, non-string "present" value. Mirror the dict legacy path: mint.
        for journal in (collections.UserDict({"new_device_id": b""}),
                        MappingProxyType({"new_device_id": b""})):
            with self.subTest(journal=type(journal).__name__):
                outcome = self._choose(journal)
                self.assertEqual(outcome[0], "returned")
                self.assertNotEqual(outcome[1], CONFIGURED_ID)


class CoercibleNonMappingTests(_Tree):
    """Things dict() happily converts must still be refused, not silently resumed."""

    def _payloads(self):
        return {
            "list of pairs": [("new_device_id", RESUMED_ID)],
            "tuple of pairs": (("new_device_id", RESUMED_ID),),
            "set of pairs": {("new_device_id", RESUMED_ID)},
            "UserList": collections.UserList([("new_device_id", RESUMED_ID)]),
            "UserString": collections.UserString(RESUMED_ID),
            "frozenset": frozenset({("new_device_id", RESUMED_ID)}),
        }

    def test_coercible_non_mappings_raise_the_mapping_error_and_touch_nothing(self) -> None:
        for label, payload in self._payloads().items():
            with self.subTest(payload=label):
                outcome = self._choose(payload)
                self.assertEqual(outcome, ("identity-error", JOURNAL_MUST_BE_MAPPING))
                self.assertEqual(self.persisted.read_text(encoding="utf-8"), CONFIGURED_ID + "\n")

    def test_coercible_payload_is_never_resumed_even_though_dict_accepts_it(self) -> None:
        # Sanity: the payload really is convertible, so refusing it is a decision, not a no-op.
        self.assertEqual(dict([("new_device_id", RESUMED_ID)])["new_device_id"], RESUMED_ID)
        self.assertNotEqual(self._choose([("new_device_id", RESUMED_ID)])[0], "returned")


class MappingContractTests(unittest.TestCase):
    def test_dict_remains_a_registered_mapping_so_the_broadened_check_keeps_dicts(self) -> None:
        # The fix relies on dict being a Mapping; if that ever stops holding, dicts would be refused.
        self.assertTrue(issubclass(dict, collections.abc.Mapping))


if __name__ == "__main__":
    unittest.main()
