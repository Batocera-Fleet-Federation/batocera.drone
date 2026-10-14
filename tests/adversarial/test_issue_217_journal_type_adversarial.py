"""Issue #217 adversarial boundaries: a journal's *type* must never choose a device id.

Expected behavior, derived from the issue before reading the patch:

- A journal that is not a real dict (a unittest mock in any spec flavour, a str, a
  list, a tuple, bytes, an int, None) must never be read as a resumed new_device_id.
- Whether such a journal is rejected with IdentityError or answered with a fresh
  MAC-shaped id is an open contract question for the identity owner (#217 out-of-scope
  finding). Both outcomes are accepted here. Never accepted: a returned id equal to the
  journal's own payload or a mock repr, or any exception other than IdentityError.
- A rejection must leave the userdata tree byte-identical: no persisted id, no
  retired-id record, no created directory.
- A rejected mock must not poison later provisioning: a real empty journal still mints
  a fresh id and persists exactly that id.
- Genuine dict subclasses are real journals and must keep resuming their id without
  rewriting the persisted file.
"""

from __future__ import annotations

import collections
import re
import tempfile
import types
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:11:22:33:44:55"
MAC_SHAPED = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def _resuming(journal: mock.MagicMock) -> mock.MagicMock:
    """A mock wired to look exactly like a journal that carries RESUMED_ID."""
    journal.get.return_value = RESUMED_ID
    journal.__getitem__.return_value = RESUMED_ID
    journal.__contains__.return_value = True
    return journal


MOCK_JOURNALS = {
    "MagicMock": lambda: mock.MagicMock(),
    "MagicMock(spec=dict)": lambda: mock.MagicMock(spec=dict),
    "Mock(spec_set=dict)": lambda: mock.Mock(spec_set=dict),
    "NonCallableMagicMock(spec=dict)": lambda: mock.NonCallableMagicMock(spec=dict),
    "MagicMock(spec=Mapping) resuming": lambda: _resuming(mock.MagicMock(spec=Mapping)),
    "MagicMock resuming": lambda: _resuming(mock.MagicMock()),
}

NON_MAPPING_PAYLOADS = {
    "MAC string": RESUMED_ID,
    "list holding MAC": [RESUMED_ID],
    "tuple holding MAC": (RESUMED_ID,),
    "bytes": RESUMED_ID.encode("ascii"),
    "int": 42,
    "bool": True,
    "None": None,
}


def _snapshot(root: Path) -> dict:
    """Every path under ``root`` mapped to its bytes (None for directories)."""
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
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.userdata = Path(self._tmp.name).resolve() / "userdata"
        (self.userdata / "system" / "drone-app").mkdir(parents=True)
        self.persisted = self.userdata / "system" / "drone-app" / "device-id"
        self.settings = types.SimpleNamespace(userdata_root=self.userdata, device_id=CONFIGURED_ID)

    def _choose(self, journal):
        return identity._choose_device_id(self.settings, journal)

    def _assert_fresh_real_id(self, chosen) -> None:
        self.assertIsInstance(chosen, str)
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertNotIn("Mock", chosen)
        self.assertNotEqual(chosen, RESUMED_ID)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertEqual(self.persisted.read_text(encoding="utf-8").strip(), chosen)


class MockJournalRejectionLeavesNoTraceTests(_Sandbox):
    def test_every_mock_journal_is_rejected_cleanly_or_minted_never_echoed(self) -> None:
        for label, make in MOCK_JOURNALS.items():
            with self.subTest(journal=label):
                before = _snapshot(self.userdata)
                try:
                    chosen = self._choose(make())
                except identity.IdentityError:
                    self.assertEqual(
                        _snapshot(self.userdata), before, "a rejected journal must not touch the userdata tree"
                    )
                    continue
                self._assert_fresh_real_id(chosen)

    def test_real_journal_after_a_rejected_mock_mints_and_persists_its_own_id(self) -> None:
        for label, make in MOCK_JOURNALS.items():
            with self.subTest(journal=label):
                try:
                    self._choose(make())
                except identity.IdentityError:
                    pass
                chosen = self._choose({})
                self._assert_fresh_real_id(chosen)
                self.assertNotEqual(chosen, RESUMED_ID)


class NonMappingPayloadTests(_Sandbox):
    def test_non_mapping_payload_never_becomes_the_device_id(self) -> None:
        for label, payload in NON_MAPPING_PAYLOADS.items():
            with self.subTest(payload=label):
                before = _snapshot(self.userdata)
                try:
                    chosen = self._choose(payload)
                except identity.IdentityError:
                    self.assertEqual(
                        _snapshot(self.userdata), before, "a rejected payload must not touch the userdata tree"
                    )
                    continue
                self._assert_fresh_real_id(chosen)


class DictSubclassResumeTests(_Sandbox):
    def test_dict_subclasses_are_real_journals_and_resume_without_rewriting(self) -> None:
        class JournalDict(dict):
            pass

        journals = (
            JournalDict(new_device_id=RESUMED_ID),
            collections.OrderedDict(new_device_id=RESUMED_ID),
            collections.defaultdict(list, new_device_id=RESUMED_ID),
        )
        for journal in journals:
            with self.subTest(journal=type(journal).__name__):
                self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
                before = _snapshot(self.userdata)
                self.assertEqual(self._choose(journal), RESUMED_ID)
                self.assertEqual(_snapshot(self.userdata), before)


if __name__ == "__main__":
    unittest.main()
