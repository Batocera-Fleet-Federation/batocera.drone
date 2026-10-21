"""Issue #286 / #454 contract: a blank USERDATA_ROOT is unset and falls back to ``/userdata``.

The #286 fix first refused an empty root with ``TypeError``, which aborted boot. #454 settled the
product decision: a blank value means "unset" (the same as every other blank override), and only a
relative root is a named configuration error. This test now pins that contract.
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

    def test_empty_userdata_root_is_treated_as_unset(self):
        # Empty means unset, so the default applies and boot is not aborted.
        with mock.patch.dict(os.environ, self._env(""), clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.userdata_root, Path("/userdata"))


if __name__ == "__main__":
    unittest.main()
