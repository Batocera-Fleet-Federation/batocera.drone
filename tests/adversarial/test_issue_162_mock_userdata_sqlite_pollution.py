"""Adversarial acceptance for issue #162 MagicMock SQLite cwd pollution.

Issue #162: a test that hands mocked Settings into database-backed code lets
SQLite treat the mock repr as a relative filename, leaving
'<MagicMock name='mock.userdata_root.__truediv__()...resolve()' ...>' (and
-shm/-wal) files in the repository root.

Expected behavior, from the issue and the db-management path rule:

- userdata_root must be a real filesystem path (str, Path, or os.PathLike).
- An unconfigured Mock/MagicMock is not a path and must be rejected before
  SQLite is opened.
- Tests may still mock Settings when DRONE_STATE_DATABASE_FILE points at a
  real file, or they must pass a real temporary userdata_root.
- .gitignore must not hide MagicMock-named files; ignoring them is how the
  pollution was papered over.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.device import notifications
from app.storage.state_store import (
    DATABASE_FILENAME,
    database_path,
    load_payload,
    open_database,
    save_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _mock_named_files(directory: Path) -> list[str]:
    names = []
    if not directory.exists():
        return names
    for path in directory.iterdir():
        name = path.name
        if "MagicMock" in name or name.startswith("<Mock") or name.startswith("<MagicMock"):
            names.append(name)
    return names


def _cwd_state_db_files(directory: Path) -> list[str]:
    db_dir = directory / "system" / "drone-app"
    if not db_dir.exists():
        return []
    return sorted(p.name for p in db_dir.iterdir() if p.is_file())


class _CwdSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmpdir.name)
        os.chdir(self.cwd)

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._tmpdir.cleanup()

    def _assert_cwd_clean(self) -> None:
        self.assertEqual(_mock_named_files(self.cwd), [])
        self.assertEqual(_cwd_state_db_files(self.cwd), [])
        self.assertEqual(_mock_named_files(REPO_ROOT), [])


class UnconfiguredMockRejectedTests(_CwdSandbox):
    def test_magicmock_userdata_root_raises_before_sqlite_connect(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("app.storage.state_store.sqlite3.connect", wraps=sqlite3.connect) as connect:
                with self.assertRaisesRegex(TypeError, "userdata_root must be a filesystem path"):
                    open_database(database_path(mock.MagicMock()))
                connect.assert_not_called()
        self._assert_cwd_clean()

    def test_mock_userdata_root_raises_before_sqlite_connect(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("app.storage.state_store.sqlite3.connect", wraps=sqlite3.connect) as connect:
                with self.assertRaisesRegex(TypeError, "userdata_root must be a filesystem path"):
                    open_database(database_path(mock.Mock()))
                connect.assert_not_called()
        self._assert_cwd_clean()

    def test_settings_userdata_root_child_mock_matches_original_filename_pattern(self) -> None:
        settings = mock.MagicMock()
        self.assertIn("userdata_root", str(settings.userdata_root))
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(TypeError, "userdata_root must be a filesystem path"):
                database_path(settings.userdata_root)
        self._assert_cwd_clean()

    def test_non_callable_magicmock_is_rejected(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(TypeError, "userdata_root must be a filesystem path"):
                database_path(mock.NonCallableMagicMock())
        self._assert_cwd_clean()

    def test_empty_drone_state_database_file_still_rejects_mocks(self) -> None:
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": ""}, clear=True):
            with self.assertRaisesRegex(TypeError, "userdata_root must be a filesystem path"):
                database_path(mock.MagicMock())
        self._assert_cwd_clean()


class NotificationsIntegrationTests(_CwdSandbox):
    def test_record_event_with_unconfigured_settings_mock_leaves_no_sqlite_files(self) -> None:
        settings = mock.MagicMock()
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("app.storage.state_store.sqlite3.connect", wraps=sqlite3.connect) as connect:
                result = notifications.record_event(
                    settings, "vpn_connected", "VPN connected", "from mocked settings"
                )
                self.assertIsNone(result)
                connect.assert_not_called()
        self._assert_cwd_clean()

    def test_record_event_uses_drone_state_database_file_when_settings_are_mocked(self) -> None:
        db = self.cwd / "configured-state.sqlite3"
        settings = mock.MagicMock()
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": str(db)}, clear=True):
            result = notifications.record_event(settings, "vpn_connected", "VPN connected", "ok")
        self.assertIsNotNone(result)
        self.assertEqual(result["event_type"], "vpn_connected")
        self.assertTrue(db.exists())
        self.assertEqual(_mock_named_files(self.cwd), [])
        self.assertEqual(_cwd_state_db_files(self.cwd), [])


class RealFilesystemPathTests(_CwdSandbox):
    def test_path_userdata_root_opens_database_under_system_drone_app(self) -> None:
        root = self.cwd / "userdata"
        with mock.patch.dict("os.environ", {}, clear=True):
            path = database_path(root)
            save_payload(path, "issue_162", {"ok": True})
            self.assertEqual(load_payload(path, "issue_162", {}), {"ok": True})
        self.assertEqual(path, (root / "system" / "drone-app" / DATABASE_FILENAME).resolve())
        self.assertTrue(path.exists())
        self.assertEqual(_mock_named_files(self.cwd), [])

    def test_str_userdata_root_is_accepted(self) -> None:
        root = self.cwd / "userdata-str"
        with mock.patch.dict("os.environ", {}, clear=True):
            path = database_path(str(root))
        self.assertEqual(path, (root / "system" / "drone-app" / DATABASE_FILENAME).resolve())

    def test_os_pathlike_userdata_root_is_accepted(self) -> None:
        root = self.cwd / "userdata-pathlike"

        class _Like(os.PathLike):
            def __fspath__(self) -> str:
                return str(root)

        with mock.patch.dict("os.environ", {}, clear=True):
            path = database_path(_Like())
            open_database(path).close()
        self.assertEqual(path, (root / "system" / "drone-app" / DATABASE_FILENAME).resolve())
        self.assertTrue(path.exists())
        self.assertEqual(_mock_named_files(self.cwd), [])

    def test_settings_from_env_with_temp_roots_does_not_pollute_cwd(self) -> None:
        root = self.cwd / "userdata"
        db = self.cwd / "state.sqlite3"
        with mock.patch.dict(
            "os.environ",
            {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(db),
                "DRONE_DEVICE_ID": "issue-162-adversarial",
            },
            clear=True,
        ):
            settings = Settings.from_env()
            save_payload(database_path(settings.userdata_root), "issue_162", {"from": "settings"})
            self.assertEqual(
                load_payload(database_path(settings.userdata_root), "issue_162", {}),
                {"from": "settings"},
            )
        self.assertTrue(db.exists())
        self.assertEqual(_mock_named_files(self.cwd), [])
        self.assertEqual(_cwd_state_db_files(self.cwd), [])


class MalformedInputTests(_CwdSandbox):
    def test_none_userdata_root_does_not_create_mock_sqlite_files(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(TypeError):
                database_path(None)  # type: ignore[arg-type]
        self._assert_cwd_clean()

    def test_int_userdata_root_does_not_create_mock_sqlite_files(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(TypeError):
                database_path(123)  # type: ignore[arg-type]
        self._assert_cwd_clean()


class GitignoreMustNotHidePollutionTests(unittest.TestCase):
    def test_gitignore_does_not_ignore_magicmock_filenames(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        for line in gitignore.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self.assertNotIn(
                "magicmock",
                stripped.lower(),
                msg=".gitignore must not hide MagicMock SQLite files; that conceals the regression",
            )
