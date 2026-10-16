"""Out-of-scope #286 evidence: an empty USERDATA_ROOT aborts boot instead of meaning "unset".

Not blocking. The issue only requires that nothing lands under the cwd. The fix achieves that by
making ``Settings.from_env`` raise ``TypeError`` for an empty root. The same function treats every
other empty env value as unset (``os.environ.get(...) or None``), and an unset ``USERDATA_ROOT``
falls back to ``/userdata``. Whether an empty value should fall back the same way is a product
decision, so these tests are expected failures that document the gap for the worker to file.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.common.settings import Settings  # noqa: E402


class OutOfScopeEmptyRootBootTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def _env(self, userdata_root: str) -> dict:
        return {
            "USERDATA_ROOT": userdata_root,
            "USE_FAKE_DATA": "1",
            # Pinned id: short-circuits the identity helpers so nothing touches /userdata.
            "DRONE_DEVICE_ID": "aa:bb:cc:dd:ee:01",
            "ROMS_ROOT": str(self.base / "roms"),
            "BIOS_ROOT": str(self.base / "bios"),
            "SAVES_ROOT": str(self.base / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.base / "state.db"),
        }

    @unittest.expectedFailure
    def test_empty_userdata_root_is_treated_as_unset(self):
        # Expected (judgment, see module docstring): empty means unset, so the default applies.
        with mock.patch.dict(os.environ, self._env(""), clear=True):
            try:
                settings = Settings.from_env()
            except TypeError as error:
                self.fail(f"empty USERDATA_ROOT aborts boot: {error}")
        self.assertEqual(settings.userdata_root, Path("/userdata"))


if __name__ == "__main__":
    unittest.main()
