"""Issue #254 adversarial: a non-Mapping must not pass the Mapping check by spoofing __class__.

The #254 fix checks ``type(journal)`` against ``collections.abc.Mapping``, not
``isinstance``. ``isinstance`` (and therefore ABC checks on ``__class__``) can be fooled by
an object whose ``__class__`` property reports UserDict or dict. Such an object is not a
real Mapping and must keep raising 'must be a mapping' without yielding a device id or
rewriting the userdata tree.

Expected, derived from the issue and the #217 mock-journal rule before inspecting the
patch: only a real Mapping implementation (its actual type) is a journal.
"""

from __future__ import annotations

import collections
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
JOURNAL_MUST_BE_MAPPING = "the identity journal must be a mapping"


class _SpoofsUserDictClass:
    """Not a Mapping, but reports itself as a UserDict via __class__."""

    @property
    def __class__(self):  # type: ignore[override]
        return collections.UserDict

    def get(self, key, default=None):
        return RESUMED_ID if key == "new_device_id" else default

    def __getitem__(self, key):
        return RESUMED_ID

    def __contains__(self, key) -> bool:
        return True


class _SpoofsDictClass:
    """Not a Mapping, but reports itself as a dict via __class__."""

    @property
    def __class__(self):  # type: ignore[override]
        return dict

    def get(self, key, default=None):
        return RESUMED_ID if key == "new_device_id" else default

    def __getitem__(self, key):
        return RESUMED_ID

    def __contains__(self, key) -> bool:
        return True


def _snapshot(root: Path) -> dict:
    entries = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        entries[key] = path.read_bytes() if path.is_file() else None
    return entries


class ClassSpoofJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cwd = Path(self._tmpdir.name).resolve()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, self._previous_cwd)
        self.userdata = self.cwd / "userdata"
        (self.userdata / "system" / "drone-app").mkdir(parents=True)
        self.persisted = self.userdata / "system" / "drone-app" / "device-id"
        self.settings = SimpleNamespace(userdata_root=self.userdata, device_id=CONFIGURED_ID)

    def _assert_rejected_without_side_effects(self, journal) -> None:
        self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        before = _snapshot(self.userdata)
        with self.assertRaises(identity.IdentityError) as ctx:
            identity._choose_device_id(self.settings, journal)
        self.assertEqual(str(ctx.exception), JOURNAL_MUST_BE_MAPPING)
        self.assertEqual(_snapshot(self.userdata), before)

    def test_object_spoofing_userdict_class_is_not_a_journal(self) -> None:
        self._assert_rejected_without_side_effects(_SpoofsUserDictClass())

    def test_object_spoofing_dict_class_is_not_a_journal(self) -> None:
        self._assert_rejected_without_side_effects(_SpoofsDictClass())

    def test_spec_userdict_mock_is_not_a_journal(self) -> None:
        journal = mock.Mock(spec=collections.UserDict)
        journal.get.return_value = RESUMED_ID
        self._assert_rejected_without_side_effects(journal)

    def test_non_callable_spec_mapping_mock_is_not_a_journal(self) -> None:
        journal = mock.NonCallableMock(spec=collections.abc.Mapping)
        self._assert_rejected_without_side_effects(journal)

    def test_real_userdict_still_resumes_while_spoofers_are_rejected(self) -> None:
        # Control: the same wiring with a genuine UserDict resumes, so the rejections above
        # are caused by the spoof, not by the journal shape.
        journal = collections.UserDict({"new_device_id": RESUMED_ID})
        self.assertEqual(identity._choose_device_id(self.settings, journal), RESUMED_ID)


if __name__ == "__main__":
    unittest.main()
