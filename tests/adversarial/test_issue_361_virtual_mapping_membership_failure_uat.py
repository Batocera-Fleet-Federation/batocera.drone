"""Issue #361: virtual Mapping journal reads must not leak implementation errors.

``Mapping.register`` accepts a promise rather than validating the complete
Mapping protocol.  Clone-resume journals are untrusted, so a registered object
whose membership operation is broken must be rejected as ``IdentityError`` and
must not fall through to generating a new device id.
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


class _RegisteredMembershipFailure:
    """Virtual Mapping with ``get`` but a broken membership read."""

    def get(self, key, default=None):
        return default

    def __contains__(self, key):
        raise AttributeError("membership backend unavailable")

    def __getitem__(self, key):
        raise AssertionError("a failed membership check must not attempt lookup")


collections.abc.Mapping.register(_RegisteredMembershipFailure)


class Issue361VirtualMappingMembershipFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = mock.patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve() / "userdata"
        self.device_id_file = root / "system" / "drone-app" / "device-id"
        self.settings = SimpleNamespace(userdata_root=root, device_id="AA:BB:CC:DD:EE:FF")

    def test_virtual_mapping_membership_attribute_error_fails_closed_without_minting(self) -> None:
        journal = _RegisteredMembershipFailure()
        self.assertTrue(issubclass(type(journal), collections.abc.Mapping))
        self.assertTrue(callable(journal.get))

        with self.assertRaisesRegex(identity.IdentityError, "identity journal must be a mapping"):
            identity._choose_device_id(self.settings, journal)

        self.assertFalse(self.device_id_file.exists())


if __name__ == "__main__":
    unittest.main()
