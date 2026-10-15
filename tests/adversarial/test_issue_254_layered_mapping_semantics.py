"""Issue #254 boundaries: layered and nested real Mappings resume; AsyncMock hybrids do not.

The filed probe is UserDict. Mapping.get looks through ChainMap overlays and
through MappingProxyType wrapping another Mapping. Those are still real
collections.abc.Mapping implementations, so a well-formed new_device_id they
expose must resume. A unittest AsyncMock that also subclasses Mapping is still
a mock (#217).

Expected, from the issue (a real Mapping is a journal) and clone-id invariants:

- ChainMap resumes the id Mapping.get would return, including when only a later
  overlay holds new_device_id.
- MappingProxyType wrapping a UserDict (or a ChainMap) resumes.
- collections.Counter and UserDict constructed from kwargs resume.
- A wrong-case or bytes key is absent: the journal mints, matching dict.
- Extra unrelated keys do not turn a UserDict into a non-journal.
- AsyncMock + Mapping still raises 'must be a mapping' and does not persist.
"""

from __future__ import annotations

import collections
import collections.abc
import os
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:11:22:33:44:55"
OTHER_ID = "02:aa:bb:cc:dd:ee"
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"
JOURNAL_MUST_BE_MAPPING = "the identity journal must be a mapping"


class _MappingStubs:
    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0


class _AsyncMockMapping(_MappingStubs, mock.AsyncMock, collections.abc.Mapping):
    """An AsyncMock that is also a Mapping subclass."""


class _Sandbox(unittest.TestCase):
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

    def _choose(self, journal):
        return identity._choose_device_id(self.settings, journal)


class LayeredMappingResumeTests(_Sandbox):
    def test_chainmap_resumes_id_from_a_later_overlay(self) -> None:
        journal = collections.ChainMap({}, {"new_device_id": RESUMED_ID})
        self.assertEqual(journal.get("new_device_id"), RESUMED_ID)
        self.assertEqual(self._choose(journal), RESUMED_ID)
        self.assertFalse(self.persisted.exists())

    def test_chainmap_first_overlay_wins_over_a_later_well_formed_id(self) -> None:
        journal = collections.ChainMap(
            {"new_device_id": RESUMED_ID},
            {"new_device_id": OTHER_ID},
        )
        self.assertEqual(self._choose(journal), RESUMED_ID)

    def test_mappingproxy_wrapping_userdict_resumes(self) -> None:
        inner = collections.UserDict({"new_device_id": RESUMED_ID, "reason": "clone"})
        self.assertEqual(self._choose(MappingProxyType(inner)), RESUMED_ID)
        self.assertFalse(self.persisted.exists())

    def test_mappingproxy_wrapping_chainmap_resumes(self) -> None:
        inner = collections.ChainMap({"new_device_id": RESUMED_ID})
        self.assertEqual(self._choose(MappingProxyType(inner)), RESUMED_ID)

    def test_counter_with_a_well_formed_id_resumes(self) -> None:
        journal = collections.Counter()
        journal["new_device_id"] = RESUMED_ID
        self.assertEqual(self._choose(journal), RESUMED_ID)
        self.assertFalse(self.persisted.exists())

    def test_userdict_from_kwargs_resumes(self) -> None:
        self.assertEqual(
            self._choose(collections.UserDict(new_device_id=RESUMED_ID)),
            RESUMED_ID,
        )

    def test_userdict_with_unrelated_keys_still_resumes(self) -> None:
        journal = collections.UserDict(
            {
                "reason": "clone",
                "state": "provisioning",
                "steps": {"retired": True},
                "new_device_id": RESUMED_ID,
                "noise": [1, 2, 3],
            }
        )
        self.assertEqual(self._choose(journal), RESUMED_ID)
        self.assertFalse(self.persisted.exists())


class AbsentKeyMintParityTests(_Sandbox):
    def test_wrong_case_key_on_userdict_mints_like_dict(self) -> None:
        chosen = self._choose(collections.UserDict({"NEW_DEVICE_ID": RESUMED_ID}))
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertNotEqual(chosen, RESUMED_ID)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertEqual(self.persisted.read_text(encoding="utf-8").strip(), chosen)

    def test_bytes_key_on_userdict_mints_like_dict(self) -> None:
        chosen = self._choose(collections.UserDict({b"new_device_id": RESUMED_ID}))
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertNotEqual(chosen, RESUMED_ID)
        self.assertEqual(self.persisted.read_text(encoding="utf-8").strip(), chosen)


class AsyncMockMappingStillRejectedTests(_Sandbox):
    def test_asyncmock_that_inherits_mapping_is_not_a_journal(self) -> None:
        journal = _AsyncMockMapping()
        journal.get = mock.Mock(return_value=RESUMED_ID)
        journal.__getitem__ = mock.Mock(return_value=RESUMED_ID)
        journal.__contains__ = mock.Mock(return_value=True)
        with self.assertRaisesRegex(identity.IdentityError, JOURNAL_MUST_BE_MAPPING):
            chosen = self._choose(journal)
            self.fail(f"AsyncMock Mapping returned {chosen!r}")
        self.assertFalse(self.persisted.exists())


if __name__ == "__main__":
    unittest.main()
