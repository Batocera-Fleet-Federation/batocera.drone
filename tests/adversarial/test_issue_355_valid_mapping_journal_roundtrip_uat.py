"""Recovery-facing acceptance coverage for issue #355.

The journal writer is part of a crash-resumable provisioning protocol.  A mapping
adapter must therefore persist a JSON object that the reader recognizes as the
same valid journal, not merely avoid ``json.dump`` raising TypeError.
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


VALID_JOURNAL = {
    "state": "PROVISIONING",
    "reason": "clone",
    "started_at": "2026-10-17T12:00:00+00:00",
    "previous_certificate_fingerprint": "a" * 64,
    "previous_device_id": "AA:BB:CC:DD:EE:FF",
    "previous_installation_id": "installation-before-clone",
    "hardware": {"version": 1, "components": {}},
    "steps": {"retired": True},
    "new_device_id": "02:11:22:33:44:55",
}


class ValidMappingJournalRoundTripUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tmpdir = tempfile.TemporaryDirectory(prefix="issue-355-roundtrip-")
        self.addCleanup(self.tmpdir.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self.tmpdir.name))

    def _assert_round_trip(self, journal) -> None:
        identity._record_journal(self.settings, journal)
        loaded = identity._read_json(identity._journal_path(self.settings))

        self.assertIsInstance(loaded, dict)
        self.assertEqual(loaded, VALID_JOURNAL)
        self.assertTrue(identity._valid_journal(loaded))

    def test_standard_mapping_adapters_round_trip_as_valid_provisioning_journals(self) -> None:
        # ChainMap's first mapping wins.  Persisting the backing maps separately,
        # or reversing their order, would resurrect the stale device id instead.
        chain_journal = collections.ChainMap(
            dict(VALID_JOURNAL),
            {"new_device_id": "66:77:88:99:AA:BB"},
        )
        journals = {
            "UserDict": collections.UserDict(VALID_JOURNAL),
            "ChainMap": chain_journal,
            "MappingProxyType": MappingProxyType(VALID_JOURNAL),
        }
        for name, journal in journals.items():
            with self.subTest(mapping=name):
                self._assert_round_trip(journal)


if __name__ == "__main__":
    unittest.main()
