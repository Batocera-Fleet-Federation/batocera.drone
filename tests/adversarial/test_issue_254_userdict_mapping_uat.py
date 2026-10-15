"""Issue #254 UAT: a real collections.UserDict journal must resume like a dict.

Issue #254 (filed from a #217 probe): identity_provisioning._choose_device_id
rejected collections.UserDict({'new_device_id': '02:11:22:33:44:55'}) with
IdentityError('the identity journal must be a mapping') even though UserDict is
a real Mapping. The error named the contract the type check did not implement.

Expected behavior, derived from the issue, docs/drone-identity.md, and the
#217/#252 mock-journal invariants *before* relying on the patch:

- A UserDict carrying a previously minted new_device_id resumes that id. It must
  not raise 'must be a mapping', must not mint a different id, and must not
  rewrite an existing persisted device-id file.
- An empty UserDict on a real userdata tree still mints a fresh locally
  administered unicast MAC-shaped id (not settings.device_id) and persists it.
- A plain dict on the same tree still resumes and mints the same way (positive
  control so a blanket UserDict special-case cannot hide a dict regression).
- A unittest MagicMock journal is still rejected: accepting real Mapping
  implementations must not re-open the #217 mock-repr resume hole.
"""

from __future__ import annotations

import collections
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:11:22:33:44:55"
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"
JOURNAL_MUST_BE_MAPPING = "the identity journal must be a mapping"


class _ChooseDeviceIdSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmpdir.name).resolve()
        os.chdir(self.cwd)
        self.userdata = self.cwd / "userdata"
        (self.userdata / "system" / "drone-app").mkdir(parents=True)
        self.persisted = self.userdata / "system" / "drone-app" / "device-id"
        self.settings = SimpleNamespace(userdata_root=self.userdata, device_id=CONFIGURED_ID)

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._tmpdir.cleanup()
        self._env.stop()

    def _choose(self, journal):
        return identity._choose_device_id(self.settings, journal)


class Issue254UserDictReproductionTests(_ChooseDeviceIdSandbox):
    """The #254 probe: UserDict with a well-formed new_device_id must resume."""

    def test_userdict_with_new_device_id_resumes_that_id(self) -> None:
        journal = collections.UserDict({"new_device_id": RESUMED_ID})
        chosen = self._choose(journal)
        self.assertEqual(chosen, RESUMED_ID)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertFalse(self.persisted.exists())

    def test_userdict_resume_does_not_raise_must_be_a_mapping(self) -> None:
        journal = collections.UserDict({"new_device_id": RESUMED_ID})
        try:
            chosen = self._choose(journal)
        except identity.IdentityError as error:
            self.fail(
                f"UserDict journal raised IdentityError({error!s}); a real Mapping "
                "must resume, and the error must not claim it is not a mapping"
            )
        self.assertEqual(chosen, RESUMED_ID)

    def test_userdict_resume_does_not_rewrite_an_existing_persisted_id(self) -> None:
        self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        chosen = self._choose(collections.UserDict({"new_device_id": RESUMED_ID}))
        self.assertEqual(chosen, RESUMED_ID)
        self.assertEqual(self.persisted.read_text(encoding="utf-8"), CONFIGURED_ID + "\n")


class Issue254UserDictMintAndDictControlTests(_ChooseDeviceIdSandbox):
    def test_empty_userdict_mints_a_fresh_id_not_the_configured_one(self) -> None:
        chosen = self._choose(collections.UserDict())
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertEqual(self.persisted.read_text(encoding="utf-8").strip(), chosen)
        first_octet = int(chosen.split(":", 1)[0], 16)
        self.assertEqual(first_octet & 0x02, 0x02, msg="fresh id must be locally administered")
        self.assertEqual(first_octet & 0x01, 0, msg="fresh id must be unicast")

    def test_dict_with_new_device_id_still_resumes(self) -> None:
        self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        chosen = self._choose({"new_device_id": RESUMED_ID})
        self.assertEqual(chosen, RESUMED_ID)
        self.assertEqual(self.persisted.read_text(encoding="utf-8"), CONFIGURED_ID + "\n")

    def test_empty_dict_still_mints(self) -> None:
        chosen = self._choose({})
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertEqual(self.persisted.read_text(encoding="utf-8").strip(), chosen)

    def test_magicmock_journal_is_still_rejected_as_not_a_mapping(self) -> None:
        with self.assertRaisesRegex(identity.IdentityError, JOURNAL_MUST_BE_MAPPING):
            self._choose(mock.MagicMock())
        self.assertFalse(self.persisted.exists())


if __name__ == "__main__":
    unittest.main()
