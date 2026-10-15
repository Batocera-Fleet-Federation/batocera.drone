"""Out-of-scope evidence for issue #254: UserDict journals cannot be persisted.

Issue #254 only covers _choose_device_id treating a real Mapping as a journal.
Production journals are json.loads dicts written by _record_journal via
json.dump. A UserDict (or ChainMap / MappingProxyType) that _choose_device_id
now accepts is not JSON-serializable, so _record_journal / _atomic_write_json
raise TypeError instead of writing identity/journal.json.

This is outside #254: no production path loads a UserDict (json.loads yields
dict), and the filed issue is the type-check vs 'must be a mapping' message on
_choose_device_id. Owner follow-up: should provisioning persist non-dict
Mapping journals, or is dict-only serialization the durable contract?

These assertions document the current serialization failure so the evidence is
retained. They are not a #254 acceptance gate.
"""

from __future__ import annotations

import collections
import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


RESUMED_ID = "02:11:22:33:44:55"


class JournalMappingJsonDumpOutOfScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.userdata = Path(self._tmpdir.name).resolve() / "userdata"
        self.userdata.mkdir()
        self.settings = SimpleNamespace(userdata_root=self.userdata, device_id="AA:BB:CC:DD:EE:FF")

    def test_json_dump_rejects_userdict_chainmap_and_mappingproxy(self) -> None:
        payloads = {
            "UserDict": collections.UserDict({"new_device_id": RESUMED_ID}),
            "ChainMap": collections.ChainMap({"new_device_id": RESUMED_ID}),
            "MappingProxyType": MappingProxyType({"new_device_id": RESUMED_ID}),
        }
        for label, payload in payloads.items():
            with self.subTest(payload=label):
                with self.assertRaises(TypeError):
                    json.dumps(payload)

    def test_record_journal_raises_typeerror_for_userdict(self) -> None:
        journal = collections.UserDict(
            {
                "state": "provisioning",
                "reason": "clone",
                "new_device_id": RESUMED_ID,
            }
        )
        with self.assertRaises(TypeError):
            identity._record_journal(self.settings, journal)
        journal_path = identity._journal_path(self.settings)
        self.assertFalse(journal_path.exists(), msg="failed persist must not leave a journal file")

    def test_record_journal_still_writes_a_plain_dict(self) -> None:
        journal = {
            "state": "provisioning",
            "reason": "clone",
            "new_device_id": RESUMED_ID,
        }
        identity._record_journal(self.settings, journal)
        stored = json.loads(identity._journal_path(self.settings).read_text(encoding="utf-8"))
        self.assertEqual(stored["new_device_id"], RESUMED_ID)
        self.assertEqual(stored["reason"], "clone")


if __name__ == "__main__":
    unittest.main()
