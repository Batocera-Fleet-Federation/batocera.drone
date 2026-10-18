"""Issue #355 acceptance: Mapping journals persist as JSON objects.

Issue #355 extends the durable journal writer contract to standard Mapping
implementations. These assertions supersede the prior #254 out-of-scope
expectation that such writes raise TypeError; json.dumps itself still accepts
only concrete JSON-compatible containers, so the journal writer must snapshot
the mapping before serializing it.
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

    def test_record_journal_persists_userdict_chainmap_and_mappingproxy(self) -> None:
        expected = {
            "state": "provisioning",
            "reason": "clone",
            "new_device_id": RESUMED_ID,
        }
        payloads = (
            collections.UserDict(expected),
            collections.ChainMap({"state": "provisioning"}, {"reason": "clone", "new_device_id": RESUMED_ID}),
            MappingProxyType(expected),
        )
        journal_path = identity._journal_path(self.settings)
        for payload in payloads:
            with self.subTest(mapping=type(payload).__name__):
                identity._record_journal(self.settings, payload)
                self.assertEqual(json.loads(journal_path.read_text(encoding="utf-8")), expected)

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
