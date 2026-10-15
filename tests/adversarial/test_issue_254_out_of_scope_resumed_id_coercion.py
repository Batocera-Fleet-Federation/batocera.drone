"""Out-of-scope evidence for issue #254: resumed-id coercion depends on the journal's own methods.

Issue #254 is about which journal types pass the 'must be a mapping' check. The two
defects below are about what _choose_device_id does with a resumed value after the
check passes. Both are outside #254 and are not reachable from production:
production journals are json.loads dicts, and json never yields a str subclass or a
Mapping whose get and __getitem__ disagree.

1. A str subclass that overrides strip() mints the id its override returns, not the
   value the journal holds. (Same for a plain dict journal; #252 territory.)
2. A Mapping whose get() reports the key present but whose __getitem__ raises KeyError
   leaks a raw KeyError from _choose_device_id instead of IdentityError.

Each defect is an expectedFailure: the suite passes while the defect exists, so it is
non-blocking evidence. It reports an unexpected success (and fails) once the defect is
fixed, which is the signal to remove the marker.
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


class _LyingStr(str):
    """A str whose normalizer-facing methods report a different id than the value holds."""

    def strip(self, *args):
        return "02:99:99:99:99:99"


class _GetSaysPresentGetItemRaises(collections.abc.Mapping):
    """A Mapping that claims the key exists via get() but cannot return it."""

    def get(self, key, default=None):
        return RESUMED_ID if key == "new_device_id" else default

    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0


class ResumedIdCoercionOutOfScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.userdata = Path(self._tmpdir.name).resolve() / "userdata"
        (self.userdata / "system" / "drone-app").mkdir(parents=True)
        self.settings = SimpleNamespace(userdata_root=self.userdata, device_id=CONFIGURED_ID)

    @unittest.expectedFailure
    def test_str_subclass_resumed_id_is_not_minted_from_its_overridden_methods(self) -> None:
        # Expected: the id is the journal's real value. Current: the override wins.
        chosen = identity._choose_device_id(
            self.settings, collections.UserDict({"new_device_id": _LyingStr(RESUMED_ID)})
        )
        self.assertEqual(chosen, RESUMED_ID)

    @unittest.expectedFailure
    def test_inconsistent_mapping_raises_identity_error_not_keyerror(self) -> None:
        # Expected: IdentityError (the journaled-id error). Current: raw KeyError.
        with self.assertRaises(identity.IdentityError):
            identity._choose_device_id(self.settings, _GetSaysPresentGetItemRaises())


if __name__ == "__main__":
    unittest.main()
