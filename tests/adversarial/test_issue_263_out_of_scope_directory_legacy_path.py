"""Out-of-scope evidence from issue #263 testing: a directory-named credentials path.

Not a blank path: ``DroneCredentialStore(<existing directory>)`` passes the file-shape check
(the directory has a real name), so ``update()`` writes the state database and only then calls
``unlink`` on the directory, which fails with an OSError after the write.  Reported for a
separate issue; this suite is evidence, not a #263 acceptance test.

Desired behaviour (same invariant as the blank-path fix): an existing directory is refused
before any SQLite write, with the same ValueError the blank path gets.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.auth import DroneCredentialStore


# Fixed in #399; the former expectedFailure marker is gone.
class DirectoryLegacyPathOutOfScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._environment = mock.patch.dict(os.environ, {}, clear=True)
        self._environment.start()
        self.addCleanup(self._environment.stop)
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.database = self.root / "state" / "injected.sqlite3"

    def test_existing_directory_credentials_path_is_refused_before_database_write(self) -> None:
        directory = self.root / "creds.d"
        directory.mkdir()
        with self.assertRaises(ValueError):
            DroneCredentialStore(directory, state_database_file=self.database).update("batocera", "longenough1")
        self.assertFalse(self.database.exists())


if __name__ == "__main__":
    unittest.main()
