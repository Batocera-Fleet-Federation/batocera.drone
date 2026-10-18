"""UAT boundaries for #359: resumed ids are data, not str-subclass behavior.

Clone provisioning resumes a durable, logical device id from its journal.  The
journal's backing string is authoritative: a str subclass may not mint an alternate
id by overriding ``strip()``, nor make invalid backing data valid.  These Python-level
inputs are deliberately adversarial (JSON itself produces plain str objects), because
the helper's Mapping API accepts an in-memory resume journal.
"""

from __future__ import annotations

import collections
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


ACTUAL_ID = "02:11:22:33:44:55"
OVERRIDE_ID = "02:99:99:99:99:99"


class _SubstitutingId(str):
    """Pretends its content is a different, otherwise valid Drone id."""

    def strip(self, *args):
        return OVERRIDE_ID


class ResumedJournalIdUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-359-uat-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "userdata"
        self.settings = SimpleNamespace(userdata_root=self.root, device_id="AA:BB:CC:DD:EE:FF")

    def test_resumed_str_subclass_uses_its_backing_value_for_dict_and_userdict(self) -> None:
        """A Mapping implementation cannot affect which id the string value represents."""
        for factory in (dict, collections.UserDict):
            with self.subTest(mapping=factory.__name__):
                with mock.patch.object(identity, "random_device_id") as mint:
                    chosen = identity._choose_device_id(
                        self.settings, factory({"new_device_id": _SubstitutingId(ACTUAL_ID)})
                    )
                self.assertEqual(chosen, ACTUAL_ID)
                self.assertNotEqual(chosen, OVERRIDE_ID)
                mint.assert_not_called()

    def test_str_subclass_cannot_make_invalid_backing_value_valid(self) -> None:
        """Validation also applies to the immutable string payload, not its override."""
        with mock.patch.object(identity, "random_device_id") as mint:
            with self.assertRaisesRegex(identity.IdentityError, "journaled device id"):
                identity._choose_device_id(
                    self.settings, {"new_device_id": _SubstitutingId("not a drone id")}
                )
        mint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
