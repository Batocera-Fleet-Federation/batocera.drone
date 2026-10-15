"""Issue #259: legacy-file DB derivation requires a file-shaped path.

The helper receives the path of a legacy JSON file, so it can derive the DB
location only when the path has a filename component. In particular, pathlib
normalizes Path('') to Path('.'); both describe the cwd and must be rejected
along with other directory-shaped paths.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage import state_store


class LegacyFilePathContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_directory_shaped_paths_are_rejected_without_deriving_parent_database(self) -> None:
        cwd = self.root / "work"
        cwd.mkdir()
        previous = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, previous)

        # Path('') has already normalized to '.', and cannot be distinguished
        # from an intentional cwd path at this API boundary.
        candidates = ("", Path(""), ".", "./", Path("."), "..", Path(".."), "/")
        for candidate in candidates:
            with self.subTest(candidate=repr(candidate)):
                with self.assertRaises(ValueError):
                    state_store.database_path_for_legacy_file(candidate)
        self.assertFalse((self.root / state_store.DATABASE_FILENAME).exists())

    def test_file_shaped_relative_and_absolute_paths_derive_their_own_parent(self) -> None:
        cases = (
            ("legacy.json", Path.cwd()),
            (self.root / "nested" / "legacy.json", self.root / "nested"),
        )
        for path, expected_parent in cases:
            with self.subTest(path=path):
                result = state_store.database_path_for_legacy_file(path)
                self.assertEqual(result, expected_parent.resolve() / state_store.DATABASE_FILENAME)
                self.assertEqual(result.name, state_store.DATABASE_FILENAME)


if __name__ == "__main__":
    unittest.main()
