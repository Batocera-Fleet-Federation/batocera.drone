"""Issue #217 UAT: a MagicMock journal must not become a resumed device id.

Issue #217 (filed from a #192 probe): with a real userdata_root (system/drone-app
present) and journal=unittest.mock.MagicMock(), identity_provisioning._choose_device_id
returned str(journal['new_device_id']) — a mock repr such as
"<MagicMock name='mock.__getitem__()' id='...'>" — instead of refusing the
non-mapping journal.

Production journals are dicts loaded from identity/journal.json. A unittest mock
is not a journal. journal.get("new_device_id") is truthy on MagicMock, so the
resume branch treated the mock as an in-flight id.

Expected behavior, derived from the issue and docs/drone-identity.md before
relying on the patch:

- A MagicMock journal with a real userdata_root raises IdentityError. It must
  not return a mock repr, must not persist one under system/drone-app/device-id,
  and must not mint-or-resume a MAC as if the mock were a valid journal.
- A mock configured to look like a resume (get/__getitem__ returning a MAC)
  is still rejected: only a real dict can carry a resumed new_device_id.
- An empty dict on the same tree still mints a fresh locally-administered
  unicast MAC-shaped id (not settings.device_id) and persists it.
- A dict with a previously minted new_device_id still resumes that id and
  does not rewrite the persisted file.
"""

from __future__ import annotations

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


def _mock_named_entries(directory: Path) -> list[str]:
    names: list[str] = []
    if not directory.exists():
        return names
    for path in directory.rglob("*"):
        name = path.name
        if "MagicMock" in name or "NonCallable" in name or name.startswith("<Mock"):
            names.append(str(path.relative_to(directory)))
    return names


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

    def _assert_no_mock_artifacts(self) -> None:
        self.assertEqual(_mock_named_entries(self.cwd), [])
        if self.persisted.exists():
            text = self.persisted.read_text(encoding="utf-8")
            self.assertNotIn("MagicMock", text)
            self.assertNotIn("NonCallable", text)
            self.assertNotIn("<Mock", text)


class Issue217MagicMockJournalReproductionTests(_ChooseDeviceIdSandbox):
    """The #217 probe: real userdata_root + MagicMock journal must not resume."""

    def test_magicmock_journal_raises_identity_error_instead_of_returning_mock_repr(self) -> None:
        with self.assertRaisesRegex(identity.IdentityError, JOURNAL_MUST_BE_MAPPING):
            self._choose(mock.MagicMock())
        self.assertFalse(self.persisted.exists())
        self._assert_no_mock_artifacts()

    def test_swallowed_identity_error_still_does_not_yield_a_mock_repr_device_id(self) -> None:
        chosen = None
        try:
            chosen = self._choose(mock.MagicMock())
        except identity.IdentityError:
            chosen = None
        self.assertIsNone(chosen)

    def test_spec_dict_magicmock_does_not_stringify_getitem_into_a_device_id(self) -> None:
        # unittest.mock sets __class__ to the spec, so isinstance(MagicMock(spec=dict), dict)
        # is True. That masquerade is still a mock journal, not a resumed id.
        journal = mock.MagicMock(spec=dict)
        chosen = None
        try:
            chosen = self._choose(journal)
        except identity.IdentityError:
            chosen = None
        except TypeError:
            chosen = None
        if chosen is not None:
            self.assertNotIn("MagicMock", str(chosen))
            self.assertNotRegex(str(chosen), r"^<MagicMock")
        self.assertIsNone(chosen)

    def test_magicmock_journal_does_not_overwrite_an_existing_persisted_id(self) -> None:
        self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        with self.assertRaises(identity.IdentityError):
            self._choose(mock.MagicMock())
        self.assertEqual(self.persisted.read_text(encoding="utf-8"), CONFIGURED_ID + "\n")
        self._assert_no_mock_artifacts()

    def test_mock_configured_as_a_resume_is_still_rejected(self) -> None:
        journal = mock.MagicMock()
        journal.get.return_value = RESUMED_ID
        journal.__getitem__.return_value = RESUMED_ID
        journal.__contains__.return_value = True
        with self.assertRaisesRegex(identity.IdentityError, JOURNAL_MUST_BE_MAPPING):
            self._choose(journal)
        self.assertFalse(self.persisted.exists())

    def test_spec_dict_mock_is_not_treated_as_a_journal_mapping(self) -> None:
        try:
            chosen = self._choose(mock.MagicMock(spec=dict))
        except identity.IdentityError as error:
            self.assertRegex(str(error), JOURNAL_MUST_BE_MAPPING)
            self.assertFalse(self.persisted.exists())
            return
        except Exception as error:
            self.fail(
                f"{type(error).__name__} leaked for MagicMock(spec=dict) instead of IdentityError: {error}"
            )
        self.fail(
            f"MagicMock(spec=dict) resumed {chosen!r}; a spec=dict mock must not be a journal mapping"
        )


class Issue217RealJournalPositiveControlTests(_ChooseDeviceIdSandbox):
    """A blanket raise on every journal would make the rejection tests pass vacuously."""

    def test_empty_dict_mints_a_fresh_id_not_the_configured_one(self) -> None:
        chosen = self._choose({})
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertNotIn("MagicMock", chosen)
        self.assertEqual(self.persisted.read_text(encoding="utf-8").strip(), chosen)
        first_octet = int(chosen.split(":", 1)[0], 16)
        self.assertEqual(first_octet & 0x02, 0x02, msg="fresh id must be locally administered")
        self.assertEqual(first_octet & 0x01, 0, msg="fresh id must be unicast")

    def test_dict_with_new_device_id_resumes_that_id_and_does_not_rewrite_the_file(self) -> None:
        self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        chosen = self._choose({"new_device_id": RESUMED_ID})
        self.assertEqual(chosen, RESUMED_ID)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertEqual(self.persisted.read_text(encoding="utf-8"), CONFIGURED_ID + "\n")

    def test_only_null_new_device_id_mints_instead_of_resuming(self) -> None:
        chosen = self._choose({"new_device_id": None})
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertNotEqual(chosen, "None")

    def test_present_falsy_malformed_new_device_id_is_rejected_without_minting(self) -> None:
        for journal in ({"new_device_id": ""}, {"new_device_id": 0}):
            with self.subTest(journal=journal):
                if self.persisted.exists():
                    self.persisted.unlink()
                with self.assertRaisesRegex(identity.IdentityError, "journaled device id"):
                    self._choose(journal)
                self.assertFalse(self.persisted.exists())


if __name__ == "__main__":
    unittest.main()
