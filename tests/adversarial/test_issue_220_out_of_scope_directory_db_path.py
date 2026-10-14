"""Out-of-scope evidence for issue #220: directory-shaped database paths reach SQLite.

This suite is deliberately separate from the in-scope #220 suites. Issue #220 covers the
*empty string*; the same "a database path must name a file" rule also covers paths that
name a directory, and those still reach ``sqlite3.connect`` and fail as
``sqlite3.OperationalError``. The finding is reported for a separate, labelled issue. It is
kept here, retained and non-blocking for #220, so the evidence is not lost.

Reproduction, observed on this branch before any change:

- ``open_database('/')`` -> ``OperationalError: unable to open database file``
- ``open_database(existing_dir)`` -> ``OperationalError: unable to open database file``
- ``open_database('..')`` -> ``OperationalError: unable to open database file``

Expected behavior if the rule were extended: ``ValueError`` before ``sqlite3.connect``.

``os.chmod`` and ``sqlite3.connect`` are patched. ``open_database`` calls
``os.chmod(path, 0o600)`` on the target, and ``chmod('/')`` would be unsafe if this ran as
root, so the test never lets a real chmod or connect touch a directory.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage.state_store import database_path_for_legacy_file, open_database


class DirectoryDatabasePathOutOfScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.work = self.root / "cwd"
        self.work.mkdir()
        self._cwd = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, self._cwd)
        connect_patch = mock.patch("sqlite3.connect")
        chmod_patch = mock.patch("os.chmod")
        self.connect = connect_patch.start()
        self.addCleanup(connect_patch.stop)
        chmod_patch.start()
        self.addCleanup(chmod_patch.stop)

    def test_directory_database_paths_are_value_errors_before_connect(self) -> None:
        candidates = ("/", "..", self.root, str(self.root))
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.connect.reset_mock()
                with self.assertRaises(ValueError):
                    open_database(candidate)
                self.connect.assert_not_called()

    def test_blank_legacy_credentials_path_is_not_derived_into_the_parent_directory(self) -> None:
        # DroneCredentialStore(Path('')) names the credentials *file* as '.', so the legacy
        # helper silently derives <cwd>.parent/rom_metadata_cache.sqlite3 instead of failing.
        with self.assertRaises(ValueError):
            database_path_for_legacy_file(Path(""))


if __name__ == "__main__":
    unittest.main()
