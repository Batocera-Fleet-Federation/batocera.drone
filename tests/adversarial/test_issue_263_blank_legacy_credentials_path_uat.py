"""Adversarial UAT for issue #263: credential files must name files, not cwd directories.

The legacy credential argument is independently safety-sensitive from the injected state
database.  A blank value normalizes to ``Path('.')``; deriving from it targets the parent
directory, while ``update`` can subsequently try to unlink the working directory.  Therefore
construction must fail before SQLite or any file mutation, even if the database path itself is
valid (or supplied by the environment).

This is deliberately a public ``DroneCredentialStore`` test.  It covers the composition that
created the regression rather than only the lower-level path helper.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.auth import DroneCredentialStore
from app.storage.state_store import DATABASE_FILENAME


class _PathLike:
    def __init__(self, value: str) -> None:
        self.value = value

    def __fspath__(self) -> str:
        return self.value


class BlankLegacyCredentialsPathUAT(unittest.TestCase):
    def setUp(self) -> None:
        self._environment = mock.patch.dict(os.environ, {}, clear=True)
        self._environment.start()
        self.addCleanup(self._environment.stop)

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.root = Path(self._temporary_directory.name).resolve()
        self.cwd = self.root / "deep" / "working-directory"
        self.cwd.mkdir(parents=True)
        self.database = self.root / "state" / "credentials.sqlite3"
        self.old_cwd = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, self.old_cwd)

    def _tree(self) -> set[Path]:
        return set(self.root.rglob("*"))

    def test_directory_spelling_is_rejected_before_any_database_or_unlink_side_effect(self) -> None:
        # Each value resolves to a directory rather than a legacy credentials file.  The final
        # absolute form exercises normalization after an otherwise non-empty path prefix.
        invalid_paths = (
            "",
            Path(""),
            ".",
            "./",
            Path("."),
            "./.",
            "..",
            Path(".."),
            str(self.cwd / ".."),
            "/",
            _PathLike(""),
            _PathLike("."),
        )
        before = self._tree()
        with mock.patch("app.storage.state_store.sqlite3.connect") as connect, mock.patch.object(
            Path, "unlink", autospec=True
        ) as unlink:
            for legacy_path in invalid_paths:
                with self.subTest(legacy_path=repr(legacy_path)):
                    with self.assertRaises(ValueError):
                        DroneCredentialStore(legacy_path, state_database_file=self.database)
        connect.assert_not_called()
        unlink.assert_not_called()
        self.assertEqual(self._tree(), before)
        self.assertFalse(self.database.exists())
        self.assertFalse((self.cwd.parent / DATABASE_FILENAME).exists())

    def test_environment_database_override_cannot_bypass_legacy_file_validation(self) -> None:
        # Environment selection is allowed for state storage, but must not turn a blank
        # credentials-file argument into an accepted directory path.
        configured_database = self.root / "configured" / "state.sqlite3"
        os.environ["DRONE_STATE_DATABASE_FILE"] = str(configured_database)
        before = self._tree()
        with mock.patch("app.storage.state_store.sqlite3.connect") as connect:
            with self.assertRaises(ValueError):
                DroneCredentialStore(Path(""))
        connect.assert_not_called()
        self.assertEqual(self._tree(), before)
        self.assertFalse(configured_database.exists())

    def test_real_credentials_file_still_updates_the_injected_database_and_is_removed(self) -> None:
        # Positive control: rejection of directory spellings must not reject an ordinary file
        # name nor disturb the explicit-database contract.
        legacy = self.cwd / "credentials.json"
        legacy.write_text('{"username": "legacy"}', encoding="utf-8")
        store = DroneCredentialStore(legacy, state_database_file=self.database)

        result = store.update("arcade-admin", "correct-horse")

        self.assertTrue(result["stored"])
        self.assertTrue(self.database.is_file())
        self.assertFalse(legacy.exists())
        self.assertTrue(store.check("arcade-admin", "correct-horse"))


if __name__ == "__main__":
    unittest.main()
