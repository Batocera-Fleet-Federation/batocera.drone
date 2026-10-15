"""Adversarial boundary tests for issue #263: a blank legacy credentials path must fail closed.

Expected behaviour, derived from the issue before reading the fix:

* A blank credentials path (empty, whitespace, format-only, or a Path spelling of those) is
  rejected by ``DroneCredentialStore`` before SQLite is touched and before any file is unlinked.
* This holds whether the state database is injected, derived, or named by a blank
  ``DRONE_STATE_DATABASE_FILE`` (an empty env value is "unset", so it must not open a bypass).
* Rejection is not over-broad: names that merely start with a dot, end in dots, or carry a
  trailing separator are real file names and keep working, and a symlink named as the
  credentials file is unlinked as a link, never followed into its target directory.

The companion UAT ``test_issue_263_blank_legacy_credentials_path_uat.py`` covers the main
directory spellings; this file widens the boundary with formatting, env, settings and
positive-control cases.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.auth import DroneCredentialStore
from app.common.settings import Settings
from app.storage.state_store import DATABASE_FILENAME


class _PathLike:
    def __init__(self, value: str) -> None:
        self.value = value

    def __fspath__(self) -> str:
        return self.value


class BlankCredentialsBoundaryAdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        self._environment = mock.patch.dict(os.environ, {}, clear=True)
        self._environment.start()
        self.addCleanup(self._environment.stop)

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.database = self.root / "state" / "injected.sqlite3"
        previous_cwd = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous_cwd)

    def _snapshot(self) -> set[Path]:
        return set(self.root.rglob("*"))

    def test_whitespace_and_format_only_paths_are_rejected_before_sqlite_or_unlink(self) -> None:
        blanks = (
            "   ",
            "\t\n",
            "​",
            "﻿",
            " ​ ",
            Path("   "),
            Path("​"),
            _PathLike(" "),
        )
        before = self._snapshot()
        with mock.patch("app.storage.state_store.sqlite3.connect") as connect, mock.patch.object(
            Path, "unlink", autospec=True
        ) as unlink:
            for blank in blanks:
                with self.subTest(blank=repr(blank)):
                    with self.assertRaises(ValueError):
                        DroneCredentialStore(blank, state_database_file=self.database)
        connect.assert_not_called()
        unlink.assert_not_called()
        self.assertEqual(self._snapshot(), before)

    def test_blank_bytes_are_rejected_without_side_effects(self) -> None:
        # Bytes are not a supported path type at all; any rejection is acceptable, derivation is not.
        before = self._snapshot()
        with mock.patch("app.storage.state_store.sqlite3.connect") as connect:
            for blank in (b"", b"."):
                with self.subTest(blank=repr(blank)):
                    with self.assertRaises((TypeError, ValueError)):
                        DroneCredentialStore(blank, state_database_file=self.database)
        connect.assert_not_called()
        self.assertEqual(self._snapshot(), before)
        self.assertFalse((self.root / DATABASE_FILENAME).exists())

    def test_blank_credentials_path_is_rejected_when_database_is_derived(self) -> None:
        with mock.patch("app.storage.state_store.sqlite3.connect") as connect:
            for blank in ("", Path(""), "."):
                with self.subTest(blank=repr(blank)):
                    with self.assertRaises(ValueError):
                        DroneCredentialStore(blank)
        connect.assert_not_called()
        self.assertFalse((self.root / DATABASE_FILENAME).exists())

    def test_blank_database_env_does_not_open_a_bypass_for_blank_credentials(self) -> None:
        # An empty DRONE_STATE_DATABASE_FILE is "unset" (falsy), so derivation still happens and
        # must still be refused; a whitespace-only value is truthy and must be refused as a database.
        for configured in ("", "   "):
            with self.subTest(configured=repr(configured)):
                os.environ["DRONE_STATE_DATABASE_FILE"] = configured
                with mock.patch("app.storage.state_store.sqlite3.connect") as connect:
                    with self.assertRaises(ValueError):
                        DroneCredentialStore(Path(""))
                connect.assert_not_called()
        self.assertFalse((self.root / DATABASE_FILENAME).exists())

    def test_blank_credentials_setting_from_env_never_derives_a_parent_database(self) -> None:
        # Settings maps DRONE_CREDENTIALS_FILE='' to a directory path. Whatever the store does
        # with it (refuse, or be pointed at the default file), it must not write beside the parent.
        userdata = self.root / "userdata"
        environment = {
            "USERDATA_ROOT": str(userdata),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_CREDENTIALS_FILE": "",
            "DRONE_DEVICE_ID": "adversarial-263",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()
        with mock.patch("app.storage.state_store.sqlite3.connect") as connect:
            try:
                DroneCredentialStore(settings.credentials_file)
            except ValueError:
                pass
        connect.assert_not_called()
        self.assertFalse((self.cwd.parent / DATABASE_FILENAME).exists())
        self.assertFalse((self.root / DATABASE_FILENAME).exists())

    def test_names_with_dots_and_trailing_separators_remain_valid_credentials_files(self) -> None:
        # Positive controls: these are real file names, so rejecting them would be over-blocking.
        for legacy in ("./creds.json", "...", ".hidden", "creds.json/"):
            with self.subTest(legacy=legacy):
                target = self.cwd / Path(legacy)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}", encoding="utf-8")
                store = DroneCredentialStore(legacy, state_database_file=self.database)
                store.update("batocera", "longenough1")
                self.assertFalse(target.exists())
                self.assertTrue(store.check("batocera", "longenough1"))

    def test_symlinked_credentials_path_unlinks_only_the_link(self) -> None:
        # A symlink named as the credentials file to an unrelated directory: update() must remove
        # the link itself and leave the directory and its contents untouched.
        real_directory = self.root / "real-directory"
        real_directory.mkdir()
        sentinel = real_directory / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        link = self.cwd / "creds.json"
        os.symlink(real_directory, link)

        store = DroneCredentialStore(Path("creds.json"), state_database_file=self.database)
        store.update("batocera", "longenough1")

        self.assertFalse(os.path.lexists(link))
        self.assertTrue(real_directory.is_dir())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertTrue(store.check("batocera", "longenough1"))


if __name__ == "__main__":
    unittest.main()
