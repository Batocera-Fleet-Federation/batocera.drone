"""Adversarial path-boundary checks for issue #260.

Path text consisting only of whitespace and Unicode format characters has no
usable visible filename. It must be rejected at the shared path guard before
SQLite is called. Format characters beside visible text remain legal filenames.
"""

from __future__ import annotations

import os
import sqlite3
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


class FormatOnlyDatabasePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _assert_rejected_before_connect(self, value):
        with mock.patch.object(state_store.sqlite3, "connect") as connect:
            with self.assertRaises(ValueError):
                state_store.open_database(value)
            connect.assert_not_called()

    def test_format_only_strings_and_pathlikes_are_rejected_at_all_path_entry_points(self):
        # Cf cases include zero-width space, BOM, word joiner, and zero-width joiner.
        values = ("\u200b", "\ufeff", "\u2060", "\u200d", " \u200b\t\ufeff ")
        for value in values:
            with self.subTest(value=value.encode("unicode_escape")):
                with mock.patch.object(state_store.sqlite3, "connect") as connect:
                    for helper in (
                        state_store.open_database,
                        state_store.database_path,
                        state_store.database_path_for_legacy_file,
                    ):
                        with self.assertRaises(ValueError):
                            helper(value)
                    connect.assert_not_called()
                self.assertEqual(list(self.root.iterdir()), [])

                self._assert_rejected_before_connect(_PathLike(value))
                self.assertEqual(list(self.root.iterdir()), [])

    def test_utf8_bytes_pathlike_with_only_format_characters_is_rejected(self):
        # os.fsdecode must reach the same blank-name decision as str input.
        self._assert_rejected_before_connect(_PathLike("\u200b".encode("utf-8")))

    def test_visible_name_with_format_characters_opens_normally(self):
        for name in ("before\u200bafter.sqlite3", "\ufeffstate.sqlite3", "state\u2060.sqlite3"):
            with self.subTest(name=name.encode("unicode_escape")):
                target = self.root / name
                connection = state_store.open_database(target)
                connection.close()
                self.assertTrue(target.is_file())
                self.assertGreater(target.stat().st_size, 0)

    def test_rejection_does_not_create_invisible_cwd_artifacts_or_block_later_open(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            self._assert_rejected_before_connect("\u200b")
            self.assertEqual(list(self.root.iterdir()), [])
            target = self.root / "valid.sqlite3"
            connection = state_store.open_database(target)
            connection.close()
            self.assertTrue(target.is_file())
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
