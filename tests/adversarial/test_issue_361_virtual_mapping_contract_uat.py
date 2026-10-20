"""Issue #361: virtually registered, incomplete Mappings fail closed.

``Mapping.register`` makes a class pass the identity journal's Mapping gate even
when it does not implement the operational Mapping interface.  A journal is
untrusted clone-recovery input: an incomplete virtual Mapping must be rejected
as ``IdentityError`` rather than leaking an implementation exception, and it
must not mint a replacement device identity.
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


class _RegisteredWithoutGet:
    """Claims Mapping membership but cannot perform the Mapping ``get`` contract."""

    def __getitem__(self, key):
        if key == "new_device_id":
            return "02:11:22:33:44:55"
        raise KeyError(key)


collections.abc.Mapping.register(_RegisteredWithoutGet)


class Issue361VirtualMappingContractTests(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve() / "userdata"
        self.device_id_file = root / "system" / "drone-app" / "device-id"
        self.settings = SimpleNamespace(userdata_root=root, device_id="AA:BB:CC:DD:EE:FF")

    def test_incomplete_virtual_mapping_is_identity_error_without_minting(self) -> None:
        journal = _RegisteredWithoutGet()
        self.assertTrue(issubclass(type(journal), collections.abc.Mapping))
        self.assertFalse(hasattr(journal, "get"))

        with self.assertRaises(identity.IdentityError):
            identity._choose_device_id(self.settings, journal)

        self.assertFalse(self.device_id_file.exists())


if __name__ == "__main__":
    unittest.main()
