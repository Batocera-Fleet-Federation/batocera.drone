"""Issue #360 UAT: inconsistent journal lookup implementations fail closed.

The journal contract permits JSON objects / real Mapping values. A key reported as
present is an attempted resume; lookup failure must be reported as an invalid
journaled id, and must not fall through to fresh identity creation.
"""

from __future__ import annotations

import collections.abc
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


RESUMED_ID = "02:11:22:33:44:55"
CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"


class _BrokenLookupMapping(collections.abc.Mapping):
    def get(self, key, default=None):
        return RESUMED_ID if key == "new_device_id" else default

    def __contains__(self, key):
        return key == "new_device_id"

    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


class _BrokenLookupDict(dict):
    """JSON-dict-shaped input with a contradictory item lookup."""

    def __contains__(self, key):
        return key == "new_device_id"

    def get(self, key, default=None):
        return RESUMED_ID if key == "new_device_id" else default

    def __getitem__(self, key):
        raise KeyError(key)


class Issue360LookupBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "userdata"
        self.device_id_file = self.root / "system" / "drone-app" / "device-id"
        self.settings = SimpleNamespace(userdata_root=self.root, device_id=CONFIGURED_ID)

    def assert_rejected_without_mint(self, journal) -> None:
        with self.assertRaisesRegex(
            identity.IdentityError, "journaled device id is not a valid Drone id"
        ):
            identity._choose_device_id(self.settings, journal)
        self.assertFalse(self.device_id_file.exists())

    def test_real_mapping_get_and_membership_cannot_mask_failed_item_lookup(self) -> None:
        journal = _BrokenLookupMapping()
        self.assertEqual(journal.get("new_device_id"), RESUMED_ID)
        self.assertIn("new_device_id", journal)
        self.assert_rejected_without_mint(journal)

    def test_dict_subclass_lookup_failure_is_not_a_fresh_id_request(self) -> None:
        journal = _BrokenLookupDict()
        self.assertEqual(journal.get("new_device_id"), RESUMED_ID)
        self.assertIn("new_device_id", journal)
        self.assert_rejected_without_mint(journal)

    def test_normal_json_object_resume_is_unchanged(self) -> None:
        journal = json.loads('{"new_device_id":"02:11:22:33:44:55"}')
        self.assertEqual(identity._choose_device_id(self.settings, journal), RESUMED_ID)
        self.assertFalse(self.device_id_file.exists())


if __name__ == "__main__":
    unittest.main()
