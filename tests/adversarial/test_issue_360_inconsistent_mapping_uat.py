"""Issue #360: inconsistent Mapping access must fail as an identity error."""

from __future__ import annotations

import collections.abc
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


RESUMED_ID = "02:11:22:33:44:55"


class GetReportsPresentButLookupRaises(collections.abc.Mapping):
    """A malformed Mapping whose membership/get view disagrees with item lookup."""

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


class InconsistentJournalMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve() / "userdata"
        (root / "system" / "drone-app").mkdir(parents=True)
        self.settings = SimpleNamespace(
            userdata_root=root, device_id="AA:BB:CC:DD:EE:FF"
        )

    def test_inconsistent_mapping_is_reported_as_invalid_journal_id(self) -> None:
        with self.assertRaisesRegex(
            identity.IdentityError, "journaled device id is not a valid Drone id"
        ):
            identity._choose_device_id(self.settings, GetReportsPresentButLookupRaises())

    def test_rejection_does_not_mint_or_write_a_device_id(self) -> None:
        with self.assertRaises(identity.IdentityError):
            identity._choose_device_id(self.settings, GetReportsPresentButLookupRaises())
        self.assertFalse((self.settings.userdata_root / "system" / "drone-app" / "device-id").exists())


if __name__ == "__main__":
    unittest.main()
