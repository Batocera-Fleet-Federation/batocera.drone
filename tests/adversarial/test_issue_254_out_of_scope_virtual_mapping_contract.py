"""Regression coverage for Issue #361's virtual-Mapping journal boundary.

The #254 fix checks ``issubclass(type(journal), collections.abc.Mapping)``, so any class
registered with ``Mapping.register`` passes the 'must be a mapping' check. Registration is a
promise that the class implements the Mapping contract. This class breaks that promise: it
has ``__getitem__`` but no ``get``, so _choose_device_id raises a raw AttributeError instead
of IdentityError. Nothing in production reaches it: production journals are json.loads dicts.

Issue #361 brings this malformed virtual-Mapping case into scope.  It must now be a
blocking regression: untrusted journal input may not leak ``AttributeError`` or mint a
replacement device identity.
"""

from __future__ import annotations

import collections.abc
import tempfile
import unittest
from types import SimpleNamespace

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:11:22:33:44:55"


class _RegisteredWithoutGet:
    """Claims to be a Mapping by registration but implements only __getitem__."""

    def __getitem__(self, key):
        if key == "new_device_id":
            return RESUMED_ID
        raise KeyError(key)


collections.abc.Mapping.register(_RegisteredWithoutGet)


class VirtualMappingContractRegressionTests(unittest.TestCase):
    def test_registered_mapping_without_get_raises_identity_error_not_attribute_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(userdata_root=tmp, device_id=CONFIGURED_ID)
            # Expected once fixed: a clean IdentityError for a journal that cannot be read.
            with self.assertRaises(identity.IdentityError):
                identity._choose_device_id(settings, _RegisteredWithoutGet())

    def test_registration_passes_the_mapping_check_today(self) -> None:
        # Pins the current evidence: the check accepts the registered class, so the
        # failure above comes from reading the journal, not from the 'must be a mapping' gate.
        self.assertTrue(issubclass(_RegisteredWithoutGet, collections.abc.Mapping))


if __name__ == "__main__":
    unittest.main()
