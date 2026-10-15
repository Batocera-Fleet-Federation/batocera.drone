"""Directory-shaped database paths must fail as path errors before SQLite.

Issue #262's contract follows from the database path naming a file: the root,
dot components, and existing directories are not database filenames. Rejecting
these as ValueError keeps a caller's path mistake distinct from SQLite I/O
failures. A normal, not-yet-created filename remains a valid database target.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage.state_store import open_database


class _PathLike:
    def __init__(self, value: str) -> None:
        self.value = value

    def __fspath__(self) -> str:
        return self.value


class DirectoryDatabasePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_cwd = os.getcwd()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        os.chdir(self.root)

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        self.temp.cleanup()

    def test_root_dot_parent_and_existing_directories_reject_before_io(self) -> None:
        existing = self.root / "existing"
        nested = existing / "nested"
        nested.mkdir(parents=True)
        directory_link = self.root / "directory-link"
        directory_link.symlink_to(existing, target_is_directory=True)

        bad_paths = (
            "/",
            "..",
            ".",
            Path("."),
            str(existing),
            str(nested),
            str(directory_link),
            _PathLike(str(existing)),
        )
        for path in bad_paths:
            with self.subTest(path=repr(path)):
                with mock.patch("app.storage.state_store.sqlite3.connect") as connect, mock.patch(
                    "app.storage.state_store.os.chmod"
                ) as chmod:
                    with self.assertRaises(ValueError):
                        open_database(path)  # type: ignore[arg-type]
                connect.assert_not_called()
                chmod.assert_not_called()

    def test_valid_filename_still_creates_parent_and_opens(self) -> None:
        target = self.root / "new-parent" / "state.sqlite3"
        with mock.patch("app.storage.state_store.sqlite3.connect", wraps=sqlite3.connect) as connect:
            connection = open_database(target)
            connection.close()
        self.assertTrue(target.is_file())
        self.assertEqual(connect.call_count, 1)


if __name__ == "__main__":
    unittest.main()
