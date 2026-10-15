"""Issue #255 boundaries: mock and non-mapping journals raise; they never mint.

Issue #255 resolves the open #217 contract question: a mock or a JSON value
that is not an object (or a real Mapping) is refused with IdentityError
('the identity journal must be a mapping'). It is never treated as a missing
journal and never used to mint a fresh device id.

The #217 suite adversarial-issue-217-journal-type-adversarial still accepts
either IdentityError or a minted id. This suite pins the owner decision:
raise, and do not persist an id.

Expected, derived from docs/drone-identity.md (Journal type contract) and the
clone-id invariant, before relying on the patch:

- json.loads of every non-object JSON value raises IdentityError and does not
  persist a device-id.
- unittest mocks, including spec=dict masquerades and mocks wired to resume a
  MAC, raise the same error and do not persist.
- Duck-typed objects with get/__getitem__ are not journals.
- An empty dict still mints (positive control: the type gate did not become a
  blanket raise). A UserDict with a well-formed id still resumes.
- A rejected journal leaves an already-persisted device-id file untouched.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections import UserDict
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:11:22:33:44:55"
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"
JOURNAL_MUST_BE_MAPPING = "the identity journal must be a mapping"

# Every JSON value json.loads can return that is not an object.
JSON_NON_OBJECT_TEXTS = (
    "[]",
    '[{"new_device_id": "%s"}]' % RESUMED_ID,
    json.dumps(RESUMED_ID),
    "null",
    "true",
    "false",
    "0",
    "7",
    "0.0",
    '""',
)


def _resuming_mock(factory):
    journal = factory()
    journal.get.return_value = RESUMED_ID
    journal.__getitem__.return_value = RESUMED_ID
    journal.__contains__.return_value = True
    return journal


class _DuckTypedJournal:
    def get(self, key, default=None):
        if key == "new_device_id":
            return RESUMED_ID
        return default

    def __getitem__(self, key):
        if key == "new_device_id":
            return RESUMED_ID
        raise KeyError(key)


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
        self.settings = SimpleNamespace(userdata_root=self.userdata, device_id=CONFIGURED_ID)

    def _choose(self, journal):
        return identity._choose_device_id(self.settings, journal)

    def _assert_raises_without_minting(self, journal) -> None:
        existed = self.persisted.exists()
        before = self.persisted.read_bytes() if existed else None
        with self.assertRaisesRegex(identity.IdentityError, JOURNAL_MUST_BE_MAPPING):
            self._choose(journal)
        if existed:
            self.assertEqual(self.persisted.read_bytes(), before)
        else:
            self.assertFalse(self.persisted.exists(), msg="rejection must not persist a minted device-id")


class JsonNonObjectJournalsRaiseAndDoNotMintTests(_Sandbox):
    def test_json_loads_non_objects_raise_identity_error_and_do_not_mint(self) -> None:
        for text in JSON_NON_OBJECT_TEXTS:
            with self.subTest(json_text=text):
                if self.persisted.exists():
                    self.persisted.unlink()
                payload = json.loads(text)
                self._assert_raises_without_minting(payload)

    def test_empty_list_is_not_an_empty_journal_and_does_not_mint(self) -> None:
        # {} mints because it is a mapping with no resumed id. [] is not a mapping.
        self._assert_raises_without_minting([])
        chosen = self._choose({})
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertEqual(self.persisted.read_text(encoding="utf-8").strip(), chosen)

    def test_json_string_mac_is_not_resumed_as_a_device_id(self) -> None:
        payload = json.loads(json.dumps(RESUMED_ID))
        self.assertEqual(payload, RESUMED_ID)
        chosen = None
        try:
            chosen = self._choose(payload)
        except identity.IdentityError:
            chosen = None
        self.assertIsNone(chosen)
        self.assertFalse(self.persisted.exists())


class MockJournalsRaiseAndDoNotMintTests(_Sandbox):
    def test_mock_flavours_raise_identity_error_never_a_fresh_id(self) -> None:
        factories = (
            mock.MagicMock,
            mock.Mock,
            mock.NonCallableMock,
            mock.NonCallableMagicMock,
            mock.AsyncMock,
            lambda: mock.MagicMock(spec=dict),
            lambda: mock.Mock(spec=dict),
            lambda: mock.create_autospec(dict, instance=True),
            lambda: _resuming_mock(mock.MagicMock),
            lambda: _resuming_mock(lambda: mock.MagicMock(spec=dict)),
        )
        for factory in factories:
            with self.subTest(factory=factory):
                if self.persisted.exists():
                    self.persisted.unlink()
                self._assert_raises_without_minting(factory())

    def test_duck_typed_non_mapping_raises_and_does_not_mint(self) -> None:
        self._assert_raises_without_minting(_DuckTypedJournal())

    def test_rejection_does_not_rewrite_an_existing_persisted_id(self) -> None:
        self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        for journal in (mock.MagicMock(), json.loads("[]"), json.loads("null"), _DuckTypedJournal()):
            with self.subTest(journal=type(journal).__name__):
                self._assert_raises_without_minting(journal)
        self.assertEqual(self.persisted.read_text(encoding="utf-8"), CONFIGURED_ID + "\n")


class MappingPositiveControlsTests(_Sandbox):
    def test_userdict_with_new_device_id_still_resumes(self) -> None:
        chosen = self._choose(UserDict({"new_device_id": RESUMED_ID}))
        self.assertEqual(chosen, RESUMED_ID)
        self.assertFalse(self.persisted.exists())

    def test_dict_with_new_device_id_still_resumes(self) -> None:
        chosen = self._choose({"new_device_id": RESUMED_ID})
        self.assertEqual(chosen, RESUMED_ID)
        self.assertFalse(self.persisted.exists())

    def test_real_empty_journal_after_a_rejected_mock_still_mints(self) -> None:
        with self.assertRaises(identity.IdentityError):
            self._choose(mock.MagicMock())
        self.assertFalse(self.persisted.exists())
        chosen = self._choose({})
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertEqual(self.persisted.read_text(encoding="utf-8").strip(), chosen)


if __name__ == "__main__":
    unittest.main()
