"""Issue #363 UAT for resumed-id and virtual Mapping edge conditions.

A present resumed identity must be validated as the value actually stored, and
malformed Python-level Mapping inputs must fail closed with IdentityError. The
fresh-id path must not run after either rejection.
"""
from __future__ import annotations

import collections.abc
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:11:22:33:44:55"


class _NoGetVirtualMapping:
    def __getitem__(self, key):
        if key == "new_device_id":
            return RESUMED_ID
        raise KeyError(key)


collections.abc.Mapping.register(_NoGetVirtualMapping)


class _BrokenMembershipVirtualMapping:
    def get(self, key, default=None):
        return default

    def __contains__(self, key):
        raise RuntimeError("membership backend failed")

    def __getitem__(self, key):
        raise AssertionError("lookup must not follow failed membership")


collections.abc.Mapping.register(_BrokenMembershipVirtualMapping)


class Issue363ResumedIdMappingEdges(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "userdata"
        self.device_id_file = self.root / "system" / "drone-app" / "device-id"
        self.settings = SimpleNamespace(userdata_root=self.root, device_id=CONFIGURED_ID)

    def assert_invalid_without_mint_or_write(self, journal):
        with mock.patch.object(identity, "random_device_id") as mint:
            with self.assertRaises(identity.IdentityError):
                identity._choose_device_id(self.settings, journal)
        mint.assert_not_called()
        self.assertFalse(self.device_id_file.exists())

    def test_virtual_mapping_without_get_is_rejected_cleanly(self):
        journal = _NoGetVirtualMapping()
        self.assertTrue(issubclass(type(journal), collections.abc.Mapping))
        self.assertFalse(callable(getattr(type(journal), "get", None)))
        self.assert_invalid_without_mint_or_write(journal)

    def test_virtual_mapping_membership_exception_is_rejected_cleanly(self):
        journal = _BrokenMembershipVirtualMapping()
        self.assertTrue(issubclass(type(journal), collections.abc.Mapping))
        self.assert_invalid_without_mint_or_write(journal)


if __name__ == "__main__":
    unittest.main()
