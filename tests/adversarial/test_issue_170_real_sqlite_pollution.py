"""End-to-end check that rejected mock paths never become SQLite files."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage.state_store import database_path_for_legacy_file, open_database


class RealSQLitePollutionTests(unittest.TestCase):
    def test_mock_paths_do_not_create_database_or_sidecars_in_cwd(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            cwd = Path(temporary_directory)
            try:
                os.chdir(cwd)
                candidates = (
                    mock.MagicMock(),
                    mock.MagicMock(spec=Path),
                    mock.Mock(spec=str),
                )
                for candidate in candidates:
                    with self.subTest(candidate_type=type(candidate).__name__):
                        with self.assertRaises(TypeError):
                            open_database(candidate)
                        with self.assertRaises(TypeError):
                            open_database(database_path_for_legacy_file(candidate))
                        self.assertEqual(list(cwd.iterdir()), [])
            finally:
                os.chdir(previous_cwd)


if __name__ == "__main__":
    unittest.main()
