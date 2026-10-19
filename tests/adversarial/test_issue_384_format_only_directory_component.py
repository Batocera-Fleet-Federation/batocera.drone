"""Adversarial path-boundary checks for issue #384.

A database path whose directory component is format-only (e.g. ``sub/<U+200B>/..``)
ends in a ``..`` directory reference. The final name is not a format character, so
the blank-name guard alone does not reject it. Before the directory-shape guard runs,
``open_database`` must not create the invisible parent directory: that directory would
be left behind and SQLite would fail later with an OperationalError instead of a
path ValueError. Every rejection here must happen before ``mkdir`` and ``connect``.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage import state_store


class _PathLike:
    def __init__(self, value):
        self.value = value

    def __fspath__(self):
        return self.value


class FormatOnlyDirectoryComponentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.previous_cwd = os.getcwd()
        os.chdir(self.root)
        self.env = mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        os.chdir(self.previous_cwd)
        self.temp.cleanup()

    def _assert_rejected_without_io(self, value):
        with mock.patch.object(state_store.sqlite3, "connect") as connect, mock.patch.object(
            state_store.Path, "mkdir"
        ) as mkdir:
            with self.assertRaises(ValueError):
                state_store.open_database(value)
            connect.assert_not_called()
            mkdir.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_format_only_component_before_dotdot_is_rejected_before_mkdir(self):
        bad_paths = (
            "sub/​/..",
            "sub/​/../",
            "sub/﻿/..",
            "sub/​/⁠/..",
            "sub/ ​\t/..",
            "a/‍/b/​/..",
        )
        for value in bad_paths:
            with self.subTest(value=value.encode("unicode_escape")):
                self._assert_rejected_without_io(value)

    def test_pathlike_and_path_object_forms_are_rejected_without_io(self):
        self._assert_rejected_without_io(_PathLike("sub/​/.."))
        self._assert_rejected_without_io(Path("sub") / "​" / "..")

    def test_existing_format_only_directory_is_not_entered_or_database_target(self):
        invisible = self.root / "sub" / "​"
        invisible.mkdir(parents=True)
        with mock.patch.object(state_store.sqlite3, "connect") as connect:
            with self.assertRaises(ValueError):
                state_store.open_database(invisible / "..")
            connect.assert_not_called()
        self.assertEqual(sorted(p.name for p in (self.root / "sub").iterdir()), ["​"])

    def test_visible_name_under_format_only_directory_still_opens(self):
        # A visible final filename stays a legitimate database target even when a
        # parent directory name is invisible; only the trailing '..' form is rejected.
        target = self.root / "sub" / "​" / "state.sqlite3"
        connection = state_store.open_database(target)
        connection.close()
        self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
