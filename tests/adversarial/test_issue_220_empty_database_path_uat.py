"""Adversarial UAT for issue #220: an empty database path must not reach SQLite.

Issue #220 (filed from #193 testing): ``DroneCredentialStore(..., state_database_file='')``
keeps the empty string (``None`` is the only derive sentinel). The state_store path
guard then accepted ``''`` because it is a ``str``. ``Path('')`` is ``Path('.')``.
``sqlite3.connect('.')`` raised ``OperationalError: unable to open database file``.
No derived ``rom_metadata_cache.sqlite3`` was created.

Expected behavior, from the issue and the db-management path rule, not from the
implementation:

- Empty and whitespace-only paths are programmer errors. ``open_database`` and the
  helpers it protects raise ``ValueError`` before ``sqlite3.connect``.
- ``Path('')`` normalizes to ``'.'``, a directory, never a database file. That value
  must not reach SQLite either.
- Construction of ``DroneCredentialStore`` still stores the injected blank path
  (issue #193). ``load`` / ``check`` / ``update`` then fail closed: no SQLite open,
  no derived credentials-adjacent database, no files in cwd.
- ``None`` (or omitted) still means "derive the default". Callers must not paper
  over a blank path with a truthiness fallback.

Cases run from an empty temporary cwd so a connect to ``'.'`` or a whitespace
filename would be visible.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.auth import DroneCredentialStore
from app.storage.state_store import (
    DATABASE_FILENAME,
    database_path_for_legacy_file,
    open_database,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _names(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted(path.name for path in directory.iterdir())


class _CwdSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmpdir.name).resolve()
        os.chdir(self.cwd)

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._tmpdir.cleanup()
        self._env.stop()

    @contextlib.contextmanager
    def _spy_sqlite_connect(self):
        real_connect = sqlite3.connect
        targets: list[str] = []

        def spy(database, *args, **kwargs):
            targets.append(str(database))
            return real_connect(database, *args, **kwargs)

        with mock.patch("app.storage.state_store.sqlite3.connect", side_effect=spy):
            yield targets

    def _credentials(self) -> Path:
        return self.cwd / "credentials.json"

    def _assert_no_sqlite(self, targets: list[str]) -> None:
        self.assertEqual(targets, [], msg=f"sqlite3.connect was called with {targets!r}")
        self.assertEqual(_names(self.cwd), [])
        self.assertFalse((self.cwd / DATABASE_FILENAME).exists())
        self.assertFalse((REPO_ROOT / DATABASE_FILENAME).exists())


class EmptyInjectedDatabasePathUatTests(_CwdSandbox):
    def test_empty_string_update_is_the_reported_reproduction(self) -> None:
        credentials = self._credentials()
        derived = database_path_for_legacy_file(credentials)
        store = DroneCredentialStore(credentials, state_database_file="")
        self.assertEqual(store.state_database_file, "")

        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(ValueError):
                store.update("arcade-admin", "correct-horse")
        self._assert_no_sqlite(targets)
        self.assertFalse(derived.exists())
        self.assertFalse(credentials.exists())

    def test_empty_string_load_and_check_also_fail_closed(self) -> None:
        store = DroneCredentialStore(self._credentials(), state_database_file="")
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(ValueError):
                store.load()
            with self.assertRaises(ValueError):
                store.check("arcade-admin", "correct-horse")
        self._assert_no_sqlite(targets)

    def test_whitespace_injected_path_does_not_create_a_space_named_database(self) -> None:
        store = DroneCredentialStore(self._credentials(), state_database_file="   ")
        self.assertEqual(store.state_database_file, "   ")
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(ValueError):
                store.update("arcade-admin", "correct-horse")
        self._assert_no_sqlite(targets)
        self.assertFalse((self.cwd / "   ").exists())
        self.assertFalse((self.cwd / "   -wal").exists())
        self.assertFalse((self.cwd / "   -shm").exists())

    def test_path_empty_injected_value_never_opens_dot_as_sqlite(self) -> None:
        store = DroneCredentialStore(self._credentials(), state_database_file=Path(""))
        self.assertEqual(store.state_database_file, Path(""))
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(ValueError):
                store.update("arcade-admin", "correct-horse")
        self._assert_no_sqlite(targets)

    def test_open_database_empty_string_is_the_direct_reproduction(self) -> None:
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(ValueError):
                open_database("")
        self._assert_no_sqlite(targets)

    def test_omitted_none_still_derives_and_persists(self) -> None:
        credentials = self._credentials()
        derived = database_path_for_legacy_file(credentials)
        omitted = DroneCredentialStore(credentials)
        explicit = DroneCredentialStore(credentials, state_database_file=None)
        self.assertEqual(omitted.state_database_file, derived)
        self.assertEqual(explicit.state_database_file, derived)

        omitted.update("arcade-admin", "correct-horse")
        self.assertTrue(omitted.check("arcade-admin", "correct-horse"))
        self.assertTrue(derived.exists())
        self.assertTrue(derived.is_file())
        self.assertFalse(credentials.exists())


if __name__ == "__main__":
    unittest.main()
