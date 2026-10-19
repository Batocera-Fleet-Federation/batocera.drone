"""Issue #254 boundaries: real Mapping journals resume; mocks and non-mappings do not.

The #254 report is UserDict. The same type-check vs 'must be a mapping' contract
applies to every stdlib Mapping that is not a dict subclass, and must not weaken
the #217 mock-journal or #252 resumed-value rules.

Expected, from the issue (a real Mapping is a journal) and clone-id invariants:

- UserDict, UserDict subclass, ChainMap, MappingProxyType, and a genuine
  collections.abc.Mapping subclass resume a well-formed new_device_id and mint
  when the key is absent.
- dict subclasses (OrderedDict, defaultdict) keep resuming.
- A UserDict whose new_device_id is not a valid Drone id raises the journaled-id
  error, not 'must be a mapping'.
- Falsy JSON-reachable new_device_id values on a UserDict still mint (legacy
  empty/null path).
- unittest mocks, including spec=dict / spec=Mapping / autospec Mapping
  masquerades, still raise 'must be a mapping' and do not stringify into an id.
- Duck-typed objects and non-mapping builtins still raise 'must be a mapping'.
- Rejection leaves the userdata tree byte-identical.
"""

from __future__ import annotations

import collections
import collections.abc
import os
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:11:22:33:44:55"
OTHER_ID = "02:aa:bb:cc:dd:ee"
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"
JOURNAL_MUST_BE_MAPPING = "the identity journal must be a mapping"
INVALID_JOURNALED_ID = "the journaled device id is not a valid Drone id"


class _UserDictSubclass(collections.UserDict):
    """A UserDict subclass is still a real Mapping, not a dict."""


class _RealMapping(collections.abc.Mapping):
    """Minimal genuine Mapping implementation (not a dict, not a mock)."""

    def __init__(self, data):
        self._data = dict(data)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


class _DuckTypedJournal:
    """Looks like a journal but is not a Mapping ABC subclass."""

    def get(self, key, default=None):
        if key == "new_device_id":
            return RESUMED_ID
        return default

    def __getitem__(self, key):
        if key == "new_device_id":
            return RESUMED_ID
        raise KeyError(key)

    def __contains__(self, key) -> bool:
        return key == "new_device_id"


def _snapshot(root: Path) -> dict:
    entries = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        entries[key] = path.read_bytes() if path.is_file() else None
    return entries


class _Sandbox(unittest.TestCase):
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

    def _choose(self, journal):
        return identity._choose_device_id(self.settings, journal)

    def _assert_fresh_mint(self, chosen) -> None:
        self.assertIsInstance(chosen, str)
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertNotEqual(chosen, RESUMED_ID)
        self.assertNotIn("Mock", chosen)
        self.assertEqual(self.persisted.read_text(encoding="utf-8").strip(), chosen)


class RealMappingResumeTests(_Sandbox):
    def test_non_dict_mappings_resume_a_well_formed_id_without_rewriting(self) -> None:
        journals = {
            "UserDict": collections.UserDict({"new_device_id": RESUMED_ID}),
            "UserDict subclass": _UserDictSubclass({"new_device_id": RESUMED_ID}),
            "ChainMap": collections.ChainMap({"new_device_id": RESUMED_ID}),
            "MappingProxyType": MappingProxyType({"new_device_id": RESUMED_ID}),
            "custom Mapping": _RealMapping({"new_device_id": RESUMED_ID}),
            "OrderedDict": collections.OrderedDict(new_device_id=RESUMED_ID),
            "defaultdict": collections.defaultdict(str, new_device_id=RESUMED_ID),
        }
        for label, journal in journals.items():
            with self.subTest(journal=label):
                self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
                before = _snapshot(self.userdata)
                self.assertEqual(self._choose(journal), RESUMED_ID)
                self.assertEqual(_snapshot(self.userdata), before)

    def test_chainmap_resumes_the_first_overlay_id(self) -> None:
        journal = collections.ChainMap(
            {"new_device_id": RESUMED_ID},
            {"new_device_id": OTHER_ID},
        )
        self.assertEqual(self._choose(journal), RESUMED_ID)
        self.assertFalse(self.persisted.exists())

    def test_empty_non_dict_mappings_mint_a_fresh_id(self) -> None:
        journals = {
            "UserDict": collections.UserDict(),
            "UserDict subclass": _UserDictSubclass(),
            "ChainMap": collections.ChainMap(),
            "MappingProxyType": MappingProxyType({}),
            "custom Mapping": _RealMapping({}),
        }
        for label, journal in journals.items():
            with self.subTest(journal=label):
                if self.persisted.exists():
                    self.persisted.unlink()
                chosen = self._choose(journal)
                self._assert_fresh_mint(chosen)
                self.persisted.unlink()


class UserDictValueBoundaryTests(_Sandbox):
    def test_invalid_resumed_id_on_userdict_is_not_a_mapping_error(self) -> None:
        invalid_values = (
            "bad id with spaces!",
            "ab",
            "x" * 129,
            "02:11:22:33:44:55\nextra",
            12345,
            ["02:11:22:33:44:55"],
            {"id": RESUMED_ID},
            True,
        )
        for value in invalid_values:
            with self.subTest(value=repr(value)):
                if self.persisted.exists():
                    self.persisted.unlink()
                with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
                    self._choose(collections.UserDict({"new_device_id": value}))
                self.assertFalse(self.persisted.exists())

    def test_mock_resumed_value_inside_userdict_is_rejected(self) -> None:
        journal = collections.UserDict({"new_device_id": mock.MagicMock()})
        with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
            self._choose(journal)
        self.assertFalse(self.persisted.exists())

    def test_only_null_new_device_id_on_userdict_mints_instead_of_resuming(self) -> None:
        chosen = self._choose(collections.UserDict({"new_device_id": None}))
        self._assert_fresh_mint(chosen)
        self.assertNotEqual(chosen, "None")

    def test_present_falsy_malformed_new_device_id_on_userdict_is_rejected(self) -> None:
        for value in ("", 0):
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
                    self._choose(collections.UserDict({"new_device_id": value}))
                self.assertFalse(self.persisted.exists())

    def test_whitespace_resumed_id_on_userdict_is_invalid_not_minted(self) -> None:
        with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
            self._choose(collections.UserDict({"new_device_id": "   "}))
        self.assertFalse(self.persisted.exists())


class MockAndNonMappingStillRejectedTests(_Sandbox):
    MOCK_JOURNALS = {
        "MagicMock": lambda: mock.MagicMock(),
        "MagicMock(spec=dict)": lambda: mock.MagicMock(spec=dict),
        "MagicMock(spec=Mapping)": lambda: mock.MagicMock(spec=collections.abc.Mapping),
        "Mock(spec_set=dict)": lambda: mock.Mock(spec_set=dict),
        "create_autospec(dict, instance=True)": lambda: mock.create_autospec(dict, instance=True),
        "create_autospec(Mapping, instance=True)": lambda: mock.create_autospec(
            collections.abc.Mapping, instance=True
        ),
        "NonCallableMagicMock(spec=dict)": lambda: mock.NonCallableMagicMock(spec=dict),
    }

    NON_MAPPINGS = {
        "None": None,
        "list": [{"new_device_id": RESUMED_ID}],
        "tuple": ("new_device_id", RESUMED_ID),
        "str MAC": RESUMED_ID,
        "bytes": b'{"new_device_id": "02:11:22:33:44:55"}',
        "int": 1,
        "bool": True,
        "set": {RESUMED_ID},
        "object": object(),
        "duck-typed": _DuckTypedJournal(),
    }

    def test_mock_journals_raise_must_be_a_mapping_and_leave_tree_alone(self) -> None:
        for label, factory in self.MOCK_JOURNALS.items():
            with self.subTest(journal=label):
                before = _snapshot(self.userdata)
                with self.assertRaisesRegex(identity.IdentityError, JOURNAL_MUST_BE_MAPPING):
                    self._choose(factory())
                self.assertEqual(_snapshot(self.userdata), before)

    def test_spec_mapping_mock_wired_as_a_resume_is_still_rejected(self) -> None:
        journal = mock.MagicMock(spec=collections.abc.Mapping)
        journal.get.return_value = RESUMED_ID
        journal.__getitem__.return_value = RESUMED_ID
        journal.__contains__.return_value = True
        with self.assertRaisesRegex(identity.IdentityError, JOURNAL_MUST_BE_MAPPING):
            self._choose(journal)
        self.assertFalse(self.persisted.exists())

    def test_non_mapping_payloads_raise_must_be_a_mapping(self) -> None:
        for label, payload in self.NON_MAPPINGS.items():
            with self.subTest(payload=label):
                before = _snapshot(self.userdata)
                with self.assertRaisesRegex(identity.IdentityError, JOURNAL_MUST_BE_MAPPING):
                    self._choose(payload)
                self.assertEqual(_snapshot(self.userdata), before)

    def test_swallowed_mock_error_does_not_yield_a_device_id(self) -> None:
        for label, factory in self.MOCK_JOURNALS.items():
            with self.subTest(journal=label):
                chosen = None
                try:
                    chosen = self._choose(factory())
                except identity.IdentityError:
                    chosen = None
                self.assertIsNone(chosen, msg=f"{label} returned {chosen!r}")


if __name__ == "__main__":
    unittest.main()
