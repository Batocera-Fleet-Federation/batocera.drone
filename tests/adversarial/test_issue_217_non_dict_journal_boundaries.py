"""Issue #217 boundaries: only a real dict journal may resume a device id.

A MagicMock is the reported case. The same resume branch also fires for any
object whose .get("new_device_id") is truthy, including other unittest mocks,
a list/str/int journal, UserDict, MappingProxyType, and a duck-typed mapping
that is not a dict. Production journals are dicts from json.loads of
journal.json; anything else must raise IdentityError rather than stringify
into a logical Drone id.

Expected, from the issue (mock or non-dict journals should raise) and the
clone-id invariant in docs/drone-identity.md:

- Every unittest.mock flavour is rejected, including spec=dict masquerades.
- Non-dict builtins and mapping-likes that are not dict raise IdentityError.
- A duck-typed object with get/__getitem__ returning a MAC still raises.
- dict subclasses (including defaultdict) still resume or mint as a dict.
- Rejection does not persist a mock-named file or rewrite an existing id.
- The error is IdentityError, not a leaked AttributeError from .get on a list.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from collections import UserDict, defaultdict
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:aa:bb:cc:dd:ee"
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"
JOURNAL_MUST_BE_MAPPING = "the identity journal must be a mapping"


class _SpecSubclass(mock.MagicMock):
    """User-defined MagicMock subclass; its type lives in this test module."""


class _DuckTypedJournal:
    """Looks like a journal mapping but is not a dict."""

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


class _JournalDict(dict):
    """Legitimate dict subclass; production json.loads is dict, but subclasses must still resume."""


MOCK_JOURNAL_FLAVOURS = {
    "MagicMock()": lambda: mock.MagicMock(),
    "Mock()": lambda: mock.Mock(),
    "NonCallableMock()": lambda: mock.NonCallableMock(),
    "NonCallableMagicMock()": lambda: mock.NonCallableMagicMock(),
    "AsyncMock()": lambda: mock.AsyncMock(),
    "MagicMock(spec=dict)": lambda: mock.MagicMock(spec=dict),
    "Mock(spec=dict)": lambda: mock.Mock(spec=dict),
    "NonCallableMock(spec=dict)": lambda: mock.NonCallableMock(spec=dict),
    "NonCallableMagicMock(spec=dict)": lambda: mock.NonCallableMagicMock(spec=dict),
    "AsyncMock(spec=dict)": lambda: mock.AsyncMock(spec=dict),
    "MagicMock(spec_set=dict)": lambda: mock.MagicMock(spec_set=dict),
    "create_autospec(dict, instance=True)": lambda: mock.create_autospec(dict, instance=True),
    "create_autospec(dict)": lambda: mock.create_autospec(dict),
    "MagicMock subclass with spec=dict": lambda: _SpecSubclass(spec=dict),
}


NON_DICT_JOURNALS = {
    "None": None,
    "list": ["new_device_id", RESUMED_ID],
    "tuple": ("new_device_id", RESUMED_ID),
    "str": RESUMED_ID,
    "empty-str": "",
    "bytes": b'{"new_device_id": "02:aa:bb:cc:dd:ee"}',
    "int": 1,
    "bool-true": True,
    "set": {RESUMED_ID},
    "object": object(),
    "UserDict": UserDict({"new_device_id": RESUMED_ID}),
    "MappingProxyType": MappingProxyType({"new_device_id": RESUMED_ID}),
    "duck-typed": _DuckTypedJournal(),
}


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

    def _assert_rejected(self, journal) -> None:
        try:
            chosen = self._choose(journal)
        except identity.IdentityError as error:
            self.assertRegex(str(error), JOURNAL_MUST_BE_MAPPING)
            self.assertFalse(self.persisted.exists(), msg="rejection must not persist a device-id")
            return
        except Exception as error:
            self.fail(
                f"{type(error).__name__} leaked instead of IdentityError: {error}"
            )
        self.fail(
            f"returned {chosen!r}; a mock or non-dict journal must raise IdentityError"
        )


class MockJournalFlavourRejectionTests(_ChooseDeviceIdSandbox):
    def test_every_mock_flavour_raises_identity_error(self) -> None:
        for label, factory in MOCK_JOURNAL_FLAVOURS.items():
            with self.subTest(journal=label):
                if self.persisted.exists():
                    self.persisted.unlink()
                self._assert_rejected(factory())

    def test_mock_flavours_do_not_return_a_stringified_mock_when_error_is_swallowed(self) -> None:
        for label, factory in MOCK_JOURNAL_FLAVOURS.items():
            with self.subTest(journal=label):
                chosen = None
                try:
                    chosen = self._choose(factory())
                except Exception:
                    chosen = None
                if chosen is not None:
                    self.assertNotIn("MagicMock", str(chosen))
                    self.assertNotEqual(chosen, CONFIGURED_ID)
                self.assertIsNone(
                    chosen,
                    msg=f"{label} returned {chosen!r}; a mock journal must not stringify into a device id",
                )


class NonDictJournalRejectionTests(_ChooseDeviceIdSandbox):
    def test_non_dict_journals_raise_identity_error_not_attributeerror(self) -> None:
        for label, journal in NON_DICT_JOURNALS.items():
            with self.subTest(journal=label):
                if self.persisted.exists():
                    self.persisted.unlink()
                try:
                    chosen = self._choose(journal)
                except identity.IdentityError as error:
                    self.assertRegex(str(error), JOURNAL_MUST_BE_MAPPING)
                    self.assertFalse(self.persisted.exists())
                    continue
                except AttributeError as error:
                    self.fail(f"{label} leaked AttributeError instead of IdentityError: {error}")
                self.fail(
                    f"{label} returned {chosen!r}; a non-dict journal must raise IdentityError"
                )

    def test_list_journal_does_not_stringify_into_a_device_id(self) -> None:
        chosen = None
        try:
            chosen = self._choose([{"new_device_id": RESUMED_ID}])
        except identity.IdentityError:
            chosen = None
        self.assertIsNone(chosen)

    def test_rejection_leaves_an_existing_persisted_id_alone(self) -> None:
        self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        for label, journal in (("MagicMock", mock.MagicMock()), ("list", []), ("UserDict", UserDict())):
            with self.subTest(journal=label):
                with self.assertRaises(identity.IdentityError):
                    self._choose(journal)
                self.assertEqual(self.persisted.read_text(encoding="utf-8"), CONFIGURED_ID + "\n")


class DictSubclassStillAJournalTests(_ChooseDeviceIdSandbox):
    def test_dict_subclass_with_new_device_id_resumes(self) -> None:
        chosen = self._choose(_JournalDict(new_device_id=RESUMED_ID))
        self.assertEqual(chosen, RESUMED_ID)
        self.assertFalse(self.persisted.exists())

    def test_defaultdict_with_new_device_id_resumes(self) -> None:
        journal = defaultdict(str)
        journal["new_device_id"] = RESUMED_ID
        chosen = self._choose(journal)
        self.assertEqual(chosen, RESUMED_ID)

    def test_dict_subclass_without_new_device_id_mints(self) -> None:
        chosen = self._choose(_JournalDict())
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertEqual(self.persisted.read_text(encoding="utf-8").strip(), chosen)


if __name__ == "__main__":
    unittest.main()
