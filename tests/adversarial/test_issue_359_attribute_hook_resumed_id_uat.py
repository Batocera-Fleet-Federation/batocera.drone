"""UAT for #359: attribute-dispatched string hooks cannot replace a resumed ID.

The journal may be supplied in memory to the resume helper.  A ``str`` subclass
can intercept lookup of ``strip`` through ``__getattribute__`` and manufacture a
different normalized value without declaring a ``strip`` method.  The durable
string payload remains authoritative: a valid payload resumes as itself, while
an invalid payload cannot become valid through that interception.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


ACTUAL_ID = "02:11:22:33:44:55"
SUBSTITUTED_ID = "02:99:99:99:99:99"


class _StripLookupSubstitutingId(str):
    """A string whose attribute lookup supplies a counterfeit normalizer hook."""

    def __getattribute__(self, name):
        if name == "strip":
            return lambda *args, **kwargs: SUBSTITUTED_ID
        return super().__getattribute__(name)


def _journal_with(value: str) -> dict:
    return {
        "reason": "clone",
        "state": identity.State.CLONE_DETECTED.value,
        "steps": {},
        "started_at": "2026-10-18T00:00:00+00:00",
        "previous_device_id": "AA:BB:CC:DD:EE:FF",
        "previous_installation_id": "previous-installation",
        "previous_certificate_fingerprint": "",
        "hardware": identity.HardwareSnapshot(
            components={"dmi_product_uuid": "a" * 64}
        ).to_dict(),
        "new_device_id": value,
    }


class AttributeHookResumedIdUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-359-attribute-hook-")
        self.addCleanup(self.tmp.cleanup)
        self.userdata = Path(self.tmp.name) / "userdata"
        self.settings = SimpleNamespace(
            userdata_root=self.userdata, device_id="AA:BB:CC:DD:EE:FF"
        )

    def test_valid_backing_payload_is_used_even_when_strip_lookup_substitutes_an_id(self) -> None:
        journal = _journal_with(_StripLookupSubstitutingId(ACTUAL_ID))

        self.assertTrue(identity._valid_journal(journal))
        with mock.patch.object(identity, "random_device_id") as mint:
            chosen = identity._choose_device_id(self.settings, journal)

        self.assertEqual(chosen, ACTUAL_ID)
        self.assertNotEqual(chosen, SUBSTITUTED_ID)
        mint.assert_not_called()

    def test_invalid_backing_payload_cannot_be_made_valid_by_strip_lookup(self) -> None:
        journal = _journal_with(_StripLookupSubstitutingId("not a drone id"))

        self.assertFalse(identity._valid_journal(journal))
        with mock.patch.object(identity, "random_device_id") as mint:
            with self.assertRaisesRegex(identity.IdentityError, "journaled device id"):
                identity._choose_device_id(self.settings, journal)

        mint.assert_not_called()
        self.assertFalse((self.userdata / "system" / "drone-app" / "device-id").exists())


if __name__ == "__main__":
    unittest.main()
