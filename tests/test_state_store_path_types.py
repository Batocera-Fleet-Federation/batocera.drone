"""State DB path helpers accept every path type the guard allows (str, PathLike)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage import state_store


class StateStorePathTypeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_open_database_accepts_str_path(self):
        connection = state_store.open_database(str(self.root / "x.sqlite3"))
        self.addCleanup(connection.close)
        self.assertTrue((self.root / "x.sqlite3").is_file())

    def test_open_database_accepts_pathlike_path(self):
        connection = state_store.open_database(self.root / "y.sqlite3")
        self.addCleanup(connection.close)
        self.assertTrue((self.root / "y.sqlite3").is_file())

    def test_database_path_for_legacy_file_accepts_str(self):
        resolved = state_store.database_path_for_legacy_file(str(self.root / "cred.json"))
        self.assertIsInstance(resolved, Path)
        self.assertEqual(resolved.parent, self.root.resolve())

    def test_database_path_for_legacy_file_accepts_pathlike(self):
        resolved = state_store.database_path_for_legacy_file(self.root / "cred.json")
        self.assertEqual(resolved.parent, self.root.resolve())

    def test_non_path_values_are_still_rejected(self):
        with self.assertRaises(TypeError):
            state_store.open_database(12345)
        with self.assertRaises(TypeError):
            state_store.database_path_for_legacy_file(None)


if __name__ == "__main__":
    unittest.main()
