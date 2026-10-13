"""Adversarial acceptance for issue #170: the os.PathLike protocol at the SQLite boundary.

Issue #170: a MagicMock reached ``sqlite3.connect`` through ``open_database`` and the
legacy-path helper. The guard must therefore key on what a value *is*, not on the
concrete ``Path`` class, while still honoring the ``os.PathLike`` contract.

Expected behavior, derived from the issue before reading the patch:

- A non-Path ``os.PathLike`` whose ``__fspath__`` returns a real string is a filesystem
  path and must open exactly that file (positive control for the PathLike contract).
- A plain ``str`` path is accepted by ``open_database``/``save_payload``/``load_payload``.
- A ``PathLike`` whose ``__fspath__`` returns a mock, or bytes, is not a filesystem path.
  It must raise ``TypeError`` before SQLite is opened and must leave the cwd empty.
- A ``PathLike`` whose ``__fspath__`` raises propagates the error before SQLite is opened.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage.state_store import (
    DATABASE_FILENAME,
    database_path_for_legacy_file,
    load_payload,
    open_database,
    save_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class _FsPath:
    """A non-Path os.PathLike with a configurable ``__fspath__`` result."""

    def __init__(self, value):
        self._value = value

    def __fspath__(self):
        return self._value


class _RaisingFsPath:
    def __fspath__(self):
        raise OSError("fspath unavailable")


def _mock_named(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return [p.name for p in directory.iterdir() if "Mock" in p.name or p.name.startswith("<")]


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


class PathLikeContractPositiveTests(_CwdSandbox):
    def test_custom_pathlike_returning_a_real_string_opens_that_exact_file(self) -> None:
        database = self.cwd / "custom" / "state.sqlite3"
        with self._spy_sqlite_connect() as targets:
            save_payload(_FsPath(str(database)), "issue_170", {"ok": True})
            self.assertEqual(load_payload(_FsPath(str(database)), "issue_170", {}), {"ok": True})
        self.assertTrue(database.exists())
        self.assertTrue(any(Path(target).resolve() == database.resolve() for target in targets))
        self.assertEqual(_mock_named(self.cwd), [])

    def test_custom_pathlike_legacy_helper_resolves_beside_the_legacy_file(self) -> None:
        legacy = _FsPath(str(self.cwd / "credentials.json"))
        self.assertEqual(database_path_for_legacy_file(legacy), self.cwd / DATABASE_FILENAME)

    def test_plain_str_path_is_accepted_by_open_database_and_payload_helpers(self) -> None:
        database = str(self.cwd / "plain" / "state.sqlite3")
        open_database(database).close()
        save_payload(database, "issue_170", {"plain": 1})
        self.assertEqual(load_payload(database, "issue_170", {}), {"plain": 1})
        self.assertTrue(Path(database).exists())
        self.assertEqual(_mock_named(self.cwd), [])


class PathLikeRejectionTests(_CwdSandbox):
    def test_pathlike_whose_fspath_returns_a_mock_is_rejected_before_sqlite(self) -> None:
        class _MockFsPath:
            def __fspath__(self):
                return mock.MagicMock()

        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(TypeError):
                open_database(_MockFsPath())
            with self.assertRaises(TypeError):
                database_path_for_legacy_file(_MockFsPath())
            with self.assertRaises(TypeError):
                save_payload(_MockFsPath(), "issue_170", {})
        self.assertEqual(targets, [])
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])

    def test_pathlike_whose_fspath_returns_bytes_is_rejected_before_sqlite(self) -> None:
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(TypeError):
                open_database(_FsPath(b"/tmp/issue-170-bytes.sqlite3"))
            with self.assertRaises(TypeError):
                database_path_for_legacy_file(_FsPath(b"/tmp/issue-170-bytes.json"))
        self.assertEqual(targets, [])
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])

    def test_pathlike_whose_fspath_raises_propagates_before_sqlite(self) -> None:
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(OSError):
                open_database(_RaisingFsPath())
        self.assertEqual(targets, [])
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
