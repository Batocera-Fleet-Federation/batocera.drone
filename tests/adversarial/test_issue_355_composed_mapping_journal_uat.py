"""Issue #355 UAT: composed standard Mapping adapters survive journal persistence.

The writer must use Mapping semantics, rather than a whitelist of the three
reported adapter types.  In particular, a read-only adapter may wrap a layered
mapping whose first layer overrides a stale value from the copied disk image.
The durable JSON object must retain that normal mapping precedence so recovery
does not resume the stale identity.
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


RESUMED_ID = "02:11:22:33:44:55"
STALE_ID = "66:77:88:99:AA:BB"
BASE_JOURNAL = {
    "state": "PROVISIONING",
    "reason": "clone",
    "started_at": "2026-10-18T12:00:00+00:00",
    "previous_certificate_fingerprint": "a" * 64,
    "previous_device_id": "AA:BB:CC:DD:EE:FF",
    "previous_installation_id": "installation-before-clone",
    "hardware": {"version": 1, "components": {}},
    "steps": {"retired": True},
    "new_device_id": STALE_ID,
}


class ComposedMappingJournalUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-355-composed-")
        self.addCleanup(self.tmp.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self.tmp.name))

    def test_readonly_layered_mapping_persists_its_effective_valid_journal(self) -> None:
        # This composition occurs naturally when an adapter exposes an overlay of
        # copied journal data.  The first map's ID is the effective Mapping value.
        journal = MappingProxyType(
            collections.ChainMap(
                MappingProxyType({"new_device_id": RESUMED_ID}),
                collections.UserDict(BASE_JOURNAL),
            )
        )

        identity._record_journal(self.settings, journal)
        stored = identity._read_json(identity._journal_path(self.settings))

        self.assertIsInstance(stored, dict)
        self.assertEqual(stored["new_device_id"], RESUMED_ID)
        self.assertEqual(stored["reason"], "clone")
        self.assertTrue(identity._valid_journal(stored))


if __name__ == "__main__":
    unittest.main()
