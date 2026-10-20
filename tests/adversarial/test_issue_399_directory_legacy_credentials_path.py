"""#399: an existing directory is refused as a legacy credentials path before any credential write."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.auth import DroneCredentialStore
from app.storage.state_store import database_path_for_legacy_file, require_legacy_file_path


class DirectoryLegacyCredentialsPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._environment = mock.patch.dict(os.environ, {}, clear=True)
        self._environment.start()
        self.addCleanup(self._environment.stop)
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.database = self.root / "state" / "injected.sqlite3"

    def test_constructor_refuses_existing_directory(self) -> None:
        directory = self.root / "creds.d"
        directory.mkdir()
        with self.assertRaises(ValueError):
            DroneCredentialStore(directory, state_database_file=self.database)
        self.assertFalse(self.database.exists())

    def test_state_store_helpers_refuse_existing_directory(self) -> None:
        directory = self.root / "creds.d"
        directory.mkdir()
        with self.assertRaises(ValueError):
            require_legacy_file_path(directory)
        with self.assertRaises(ValueError):
            database_path_for_legacy_file(directory)
        self.assertFalse((self.root / "state.sqlite3").exists())

    def test_path_swapped_for_directory_after_construction_fails_before_any_write(self) -> None:
        legacy = self.root / "creds.json"
        legacy.write_text("{}", encoding="utf-8")
        store = DroneCredentialStore(legacy, state_database_file=self.database)
        legacy.unlink()
        legacy.mkdir()
        with self.assertRaises(ValueError):
            store.update("batocera", "longenough1")
        self.assertFalse(self.database.exists())
        self.assertTrue(legacy.is_dir())

    def test_failed_update_leaves_existing_credentials_unchanged(self) -> None:
        legacy = self.root / "creds.json"
        store = DroneCredentialStore(legacy, state_database_file=self.database)
        store.update("batocera", "firstpassword1")
        legacy.mkdir()
        with self.assertRaises(ValueError):
            store.update("batocera", "secondpassword2")
        self.assertTrue(store.check("batocera", "firstpassword1"))
        self.assertFalse(store.check("batocera", "secondpassword2"))

    def test_symlink_to_directory_is_unlinked_without_touching_the_directory(self) -> None:
        target = self.root / "real.d"
        target.mkdir()
        sentinel = target / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        link = self.root / "creds.link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        store = DroneCredentialStore(link, state_database_file=self.database)
        self.assertTrue(store.update("batocera", "longenough1")["stored"])
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_symlink_to_file_is_replaced_without_touching_its_target(self) -> None:
        target = self.root / "real.json"
        target.write_text("{}", encoding="utf-8")
        link = self.root / "creds.link"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        store = DroneCredentialStore(link, state_database_file=self.database)
        self.assertTrue(store.update("batocera", "longenough1")["stored"])
        self.assertFalse(link.is_symlink())
        self.assertTrue(target.exists())

    def test_dangling_symlink_and_regular_file_still_work(self) -> None:
        dangling = self.root / "dangling.link"
        try:
            dangling.symlink_to(self.root / "missing.json")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        regular = self.root / "creds.json"
        regular.write_text("{}", encoding="utf-8")
        for path in (dangling, regular, self.root / "absent.json"):
            with self.subTest(path=path.name):
                shutil.rmtree(self.database.parent, ignore_errors=True)
                store = DroneCredentialStore(path, state_database_file=self.database)
                self.assertTrue(store.update("batocera", "longenough1")["stored"])
                self.assertFalse(path.exists() or path.is_symlink())
                with sqlite3.connect(self.database) as connection:
                    self.assertTrue(connection.execute("SELECT 1 FROM sqlite_master").fetchall())


if __name__ == "__main__":
    unittest.main()
