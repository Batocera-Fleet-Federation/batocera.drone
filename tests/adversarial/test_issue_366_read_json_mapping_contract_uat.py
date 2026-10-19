"""Issue #366 UAT: the on-disk journal loader is dict-only by contract.

Issue #366 asked whether _read_json should accept any Mapping (such as a
collections.UserDict decoded from a journal) or whether dict-only decoding is the
durable journal contract. Decision: dict-only. Reasons, all from the code and
docs/drone-identity.md:

- json.loads of a JSON object always yields a dict, so the on-disk path never
  produces a UserDict in production. Widening the loader adds no real input.
- The disk-path gates (_ensure_locked, the admin "new" action, the recovery
  checks) validate only when isinstance(journal, dict). A Mapping-accepting
  loader would let a non-dict journal skip those checks entirely.
- The documented contract is that a journal which is not a JSON object is treated
  like a torn journal: re-derive from the binding and hardware, never resume it.

These tests pin that decision end to end, with the real file on disk. The
Python-API Mapping acceptance in _choose_device_id (#254) is tested separately
and stays as it is.
"""

from __future__ import annotations

import collections
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


REAL_LOADS = json.loads
CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:11:22:33:44:55"
PAYLOAD = {"new_device_id": RESUMED_ID, "reason": "clone", "state": "provisioning"}


class ReadJsonMappingContractUatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.userdata = Path(self._tmpdir.name).resolve() / "userdata"
        (self.userdata / "system" / "drone-app").mkdir(parents=True)
        self.settings = SimpleNamespace(userdata_root=self.userdata, device_id=CONFIGURED_ID)
        self.journal = identity._journal_path(self.settings)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.persisted = self.userdata / "system" / "drone-app" / "device-id"

    def test_real_journal_object_on_disk_loads_as_dict_and_resumes(self) -> None:
        identity._record_journal(self.settings, dict(PAYLOAD))
        loaded = identity._read_json(self.journal)
        self.assertIsInstance(loaded, dict)
        self.assertIs(type(loaded), dict)
        self.assertIsNot(loaded, identity._CORRUPT)
        chosen = identity._choose_device_id(self.settings, loaded)
        self.assertEqual(chosen, RESUMED_ID)
        self.assertNotEqual(chosen, CONFIGURED_ID)

    def test_userdict_decoded_journal_is_corrupt_on_the_disk_path(self) -> None:
        # The loader is the only place a decoded value enters the identity flow, so a
        # Mapping that is not a dict must be classified as corrupt before any resume.
        self.journal.write_text(json.dumps(PAYLOAD), encoding="utf-8")
        with mock.patch.object(
            identity.json, "loads", side_effect=lambda text: collections.UserDict(REAL_LOADS(text))
        ):
            loaded = identity._read_json(self.journal)
        self.assertIs(loaded, identity._CORRUPT)

    def test_corrupt_journal_is_never_resumed_or_minted_from(self) -> None:
        # A corrupt journal must not become a device id. A caller that only sees
        # _CORRUPT has nothing to resume, so the run re-derives from the binding.
        self.journal.write_text(json.dumps(PAYLOAD), encoding="utf-8")
        with mock.patch.object(
            identity.json, "loads", side_effect=lambda text: collections.UserDict(REAL_LOADS(text))
        ):
            loaded = identity._read_json(self.journal)
        self.assertIs(loaded, identity._CORRUPT)
        self.assertFalse(self.persisted.exists())

    def test_non_object_json_on_disk_is_corrupt_not_absent(self) -> None:
        for body in ("[1, 2]", '"text"', "42", "true", "null"):
            with self.subTest(body=body):
                self.journal.write_text(body, encoding="utf-8")
                self.assertIs(identity._read_json(self.journal), identity._CORRUPT)

    def test_missing_journal_stays_absent_not_corrupt(self) -> None:
        self.assertIs(identity._read_json(self.journal), identity._ABSENT)


if __name__ == "__main__":
    unittest.main()
