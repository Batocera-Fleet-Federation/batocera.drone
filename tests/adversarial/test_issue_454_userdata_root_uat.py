"""Issue #454 UAT: blank USERDATA_ROOT means unset; relative roots fail with a clear message.

An empty ``USERDATA_ROOT`` used to produce ``Path("")`` (the working directory) and abort
boot with an identity TypeError. Blank values now behave like unset and fall back to
``/userdata``. A relative root is still refused (identity must never live under cwd), but
the refusal is a named configuration error raised by ``Settings.from_env()``.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings


class UserdataRootBlankAndRelativeUatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="issue-454-uat-")
        self.work = Path(self._tmpdir.name).resolve()
        self.cwd = self.work / "process-cwd"
        self.cwd.mkdir()
        os.chdir(self.cwd)

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._tmpdir.cleanup()

    def _env(self, **overrides: str) -> dict:
        env = {
            "DRONE_DEVICE_ID": "02:00:00:00:04:54",
            "USE_FAKE_DATA": "1",
        }
        env.update(overrides)
        return env

    def test_empty_and_blank_userdata_root_fall_back_to_default(self) -> None:
        for value in ("", "   "):
            with self.subTest(value=repr(value)):
                with mock.patch.dict("os.environ", self._env(USERDATA_ROOT=value), clear=True):
                    settings = Settings.from_env()
                self.assertEqual(settings.userdata_root, Path("/userdata"))
                self.assertTrue(settings.userdata_root.is_absolute())

    def test_empty_userdata_root_boots_with_fake_data_and_no_cwd_writes(self) -> None:
        with mock.patch.dict("os.environ", self._env(USERDATA_ROOT="", USE_FAKE_DATA="0"), clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.userdata_root, Path("/userdata"))
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_unset_userdata_root_still_defaults(self) -> None:
        with mock.patch.dict("os.environ", self._env(), clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.userdata_root, Path("/userdata"))

    def test_relative_userdata_root_is_a_named_configuration_error(self) -> None:
        with mock.patch.dict("os.environ", self._env(USERDATA_ROOT="local-data"), clear=True):
            with self.assertRaises(ValueError) as ctx:
                Settings.from_env()
        message = str(ctx.exception)
        self.assertIn("USERDATA_ROOT must be an absolute path", message)
        self.assertIn("local-data", message)
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_dot_userdata_root_is_rejected_not_treated_as_cwd(self) -> None:
        with mock.patch.dict("os.environ", self._env(USERDATA_ROOT="."), clear=True):
            with self.assertRaisesRegex(ValueError, "absolute path"):
                Settings.from_env()

    def test_absolute_userdata_root_is_used_verbatim(self) -> None:
        root = self.work / "userdata"
        with mock.patch.dict("os.environ", self._env(USERDATA_ROOT=str(root)), clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.userdata_root, root)
        self.assertEqual(list(self.cwd.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
