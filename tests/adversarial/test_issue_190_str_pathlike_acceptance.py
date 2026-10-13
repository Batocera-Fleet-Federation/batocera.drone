"""Adversarial acceptance for issue #190: str and PathLike must not AttributeError.

Issue #190: ``open_database('/tmp/x.sqlite3')`` and
``database_path_for_legacy_file('/tmp/cred.json')`` passed ``_require_filesystem_path``
(which allows ``str`` and ``os.PathLike``) then crashed with
``AttributeError: 'str' object has no attribute 'parent' / 'resolve'``.

Expected behavior, derived from the issue, the public signatures
(``PathLike[str] | str``), and the db-management path rule, before treating the
delivery tests as ground truth:

- A plain ``str`` is a filesystem path. Both helpers must succeed. The failure
  mode is not ``AttributeError``.
- A non-``Path`` ``os.PathLike`` whose ``__fspath__`` returns a ``str`` is also a
  filesystem path. ``pathlib.Path`` is insufficient coverage: it already has
  ``.parent`` and ``.resolve``, so it never reproduced the bug.
- ``pathlib.PurePosixPath`` is PathLike, has ``.parent``, and has no ``.resolve``.
  The legacy helper must still return a real ``Path`` beside that file.
- Missing parent directories are created. SQLite and WAL sidecars land at the
  given path, not as mock-named files in the cwd.
- Wrappers that pass their first argument to ``open_database`` (``save_payload``,
  ``load_payload``) inherit str/PathLike acceptance.
- ``DRONE_STATE_DATABASE_FILE`` still overrides the legacy helper when set, and a
  falsy override still uses the str argument.

Every case runs from an empty temporary cwd with a cleared environment.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from app.storage.state_store import (
    DATABASE_FILENAME,
    database_path,
    database_path_for_legacy_file,
    load_payload,
    open_database,
    save_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class _FsPath:
    """os.PathLike that is not pathlib.Path and has no .parent / .resolve."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __fspath__(self) -> str:
        return self._value


class _StrPath(str):
    """str subclass; isinstance(..., str) is True."""


def _mock_named(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return [
        path.name
        for path in directory.iterdir()
        if "Mock" in path.name or path.name.startswith("<")
    ]


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


class Issue190StrReproductionTests(_CwdSandbox):
    def test_open_database_accepts_absolute_str_without_attributeerror(self) -> None:
        # Shape of the reported repro: open_database('/tmp/x.sqlite3') on a real str.
        database = self.cwd / "nested" / "x.sqlite3"
        with self._spy_sqlite_connect() as targets:
            connection = open_database(str(database))
            try:
                self.assertIsInstance(connection, sqlite3.Connection)
            finally:
                connection.close()
        self.assertTrue(database.is_file())
        self.assertTrue(any(Path(target).resolve() == database.resolve() for target in targets))
        self.assertEqual(_mock_named(self.cwd), [])
        self.assertEqual(_mock_named(REPO_ROOT), [])

    def test_legacy_helper_accepts_absolute_str_and_returns_path_beside_file(self) -> None:
        # Shape of the reported repro: database_path_for_legacy_file('/tmp/cred.json').
        credentials = str(self.cwd / "auth" / "cred.json")
        resolved = database_path_for_legacy_file(credentials)
        self.assertIsInstance(resolved, Path)
        self.assertNotIsInstance(resolved, mock.Mock)
        self.assertEqual(resolved, (self.cwd / "auth" / DATABASE_FILENAME).resolve())
        self.assertEqual(resolved.parent, (self.cwd / "auth").resolve())

    def test_open_database_creates_missing_parents_for_str(self) -> None:
        database = self.cwd / "a" / "b" / "c" / "state.sqlite3"
        self.assertFalse(database.parent.exists())
        open_database(str(database)).close()
        self.assertTrue(database.is_file())

    def test_relative_str_opens_under_cwd_not_repo_root(self) -> None:
        relative = "rel/state.sqlite3"
        with self._spy_sqlite_connect() as targets:
            open_database(relative).close()
        expected = (self.cwd / relative).resolve()
        self.assertTrue(expected.is_file())
        self.assertFalse((REPO_ROOT / relative).exists())
        self.assertTrue(any(Path(target).resolve() == expected for target in targets))

    def test_str_subclass_is_accepted(self) -> None:
        database = _StrPath(str(self.cwd / "subclass.sqlite3"))
        open_database(database).close()
        self.assertTrue(Path(database).is_file())
        resolved = database_path_for_legacy_file(_StrPath(str(self.cwd / "cred.json")))
        self.assertEqual(resolved.parent, self.cwd)

    def test_dotdot_relative_str_resolves_beside_the_legacy_file(self) -> None:
        nested = self.cwd / "keep" / "inner"
        nested.mkdir(parents=True)
        credentials = os.path.join("keep", "inner", "..", "cred.json")
        resolved = database_path_for_legacy_file(credentials)
        self.assertEqual(resolved, (self.cwd / "keep" / DATABASE_FILENAME).resolve())

    def test_unicode_str_path_roundtrips_payload(self) -> None:
        database = str(self.cwd / "ünicode" / "state.sqlite3")
        save_payload(database, "issue_190", {"ok": "café"})
        self.assertEqual(load_payload(database, "issue_190", {}), {"ok": "café"})
        self.assertTrue(Path(database).is_file())


class Issue190NonPathPathLikeTests(_CwdSandbox):
    def test_custom_pathlike_without_parent_or_resolve_opens_that_file(self) -> None:
        database = self.cwd / "custom" / "state.sqlite3"
        value = _FsPath(str(database))
        self.assertFalse(hasattr(value, "parent"))
        self.assertFalse(hasattr(value, "resolve"))
        with self._spy_sqlite_connect() as targets:
            open_database(value).close()
        self.assertTrue(database.is_file())
        self.assertTrue(any(Path(target).resolve() == database.resolve() for target in targets))

    def test_custom_pathlike_legacy_helper_does_not_need_resolve(self) -> None:
        credentials = _FsPath(str(self.cwd / "peer" / "cred.json"))
        self.assertFalse(hasattr(credentials, "resolve"))
        resolved = database_path_for_legacy_file(credentials)
        self.assertEqual(resolved, (self.cwd / "peer" / DATABASE_FILENAME).resolve())

    def test_pure_posix_path_legacy_helper_survives_missing_resolve(self) -> None:
        credentials = PurePosixPath(self.cwd / "pure" / "cred.json")
        self.assertFalse(hasattr(credentials, "resolve"))
        resolved = database_path_for_legacy_file(credentials)
        self.assertIsInstance(resolved, Path)
        self.assertEqual(resolved, (Path(self.cwd) / "pure" / DATABASE_FILENAME).resolve())

    def test_pure_posix_path_open_database_creates_the_file(self) -> None:
        database = PurePosixPath(self.cwd / "pure" / "state.sqlite3")
        open_database(database).close()
        self.assertTrue(Path(database).is_file())


class Issue190CompositionAndWrapperTests(_CwdSandbox):
    def test_str_legacy_path_composed_into_open_database(self) -> None:
        credentials = str(self.cwd / "system" / "drone-app" / "credentials.json")
        db_path = database_path_for_legacy_file(credentials)
        with self._spy_sqlite_connect() as targets:
            open_database(db_path).close()
        expected = (self.cwd / "system" / "drone-app" / DATABASE_FILENAME).resolve()
        self.assertTrue(expected.is_file())
        self.assertTrue(any(Path(target).resolve() == expected for target in targets))

    def test_save_and_load_payload_accept_str_and_custom_pathlike(self) -> None:
        as_str = str(self.cwd / "wrap" / "state.sqlite3")
        save_payload(as_str, "issue_190", {"via": "str"})
        self.assertEqual(load_payload(as_str, "issue_190", {}), {"via": "str"})

        as_like = _FsPath(str(self.cwd / "wrap" / "like.sqlite3"))
        save_payload(as_like, "issue_190", {"via": "pathlike"})
        self.assertEqual(load_payload(as_like, "issue_190", {}), {"via": "pathlike"})

    def test_database_path_accepts_str_userdata_root(self) -> None:
        resolved = database_path(str(self.cwd / "userdata"))
        self.assertEqual(
            resolved,
            (self.cwd / "userdata" / "system" / "drone-app" / DATABASE_FILENAME).resolve(),
        )

    def test_str_created_database_is_owner_rw_only_and_uses_wal(self) -> None:
        database = self.cwd / "perm" / "state.sqlite3"
        save_payload(str(database), "issue_190", {"perm": True})
        connection = sqlite3.connect(str(database))
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(str(mode).lower(), "wal")
        file_mode = stat.S_IMODE(database.stat().st_mode)
        self.assertEqual(file_mode, 0o600)
        self.assertEqual(_mock_named(self.cwd), [])

    def test_falsy_override_still_uses_the_str_legacy_argument(self) -> None:
        credentials = str(self.cwd / "creds" / "credentials.json")
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": ""}, clear=True):
            resolved = database_path_for_legacy_file(credentials)
        self.assertEqual(resolved, (self.cwd / "creds" / DATABASE_FILENAME).resolve())

    def test_configured_override_wins_over_str_legacy_argument(self) -> None:
        configured = self.cwd / "override.sqlite3"
        with mock.patch.dict(
            "os.environ",
            {"DRONE_STATE_DATABASE_FILE": str(configured)},
            clear=True,
        ):
            resolved = database_path_for_legacy_file(str(self.cwd / "ignored" / "cred.json"))
        self.assertEqual(resolved, configured.resolve())
        self.assertFalse((self.cwd / "ignored").exists())


if __name__ == "__main__":
    unittest.main()
