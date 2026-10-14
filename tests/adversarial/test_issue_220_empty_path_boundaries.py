"""Boundary, malformed, and error-path checks for issue #220.

Issue #220: the filesystem-path guard accepted an empty string, so
``open_database('')`` became ``sqlite3.connect('.')``. Whitespace-only strings
would instead have created a space-named SQLite file plus WAL/SHM sidecars.

Expected behavior, from the empty-path contract (not from the current branch):

- Every empty or whitespace-only ``str`` / ``os.PathLike`` is ``ValueError`` on
  ``open_database``, ``database_path``, and ``database_path_for_legacy_file``.
- Wrappers that pass their first argument to ``open_database`` inherit that
  rejection and do not migrate or unlink a legacy JSON file.
- ``Path('')`` and ``'.'`` name the current directory, never a database file;
  ``open_database`` rejects them before connect. ``database_path('.')`` is a
  userdata *root* (a directory) and must still derive a real file path.
- Non-path values stay ``TypeError`` (issue #190 / #193). Empty ``str`` is a
  path-shaped value, so emptiness is ``ValueError``, not a type error.
- A later valid path still opens after a rejected blank path.

Cases run from an empty temporary cwd so stray connect targets are observable.
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
    append_event,
    database_path,
    database_path_for_legacy_file,
    load_events,
    load_payload,
    open_database,
    save_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[2]

BLANK_STRINGS = ("", " ", "   ", "\t", "\n", "\r", "\t\n", " \t \n ")


class _FsPath:
    def __init__(self, value) -> None:
        self._value = value

    def __fspath__(self):
        return self._value


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

    def _assert_clean(self, targets: list[str]) -> None:
        self.assertEqual(targets, [], msg=f"sqlite3.connect was called with {targets!r}")
        self.assertEqual(_names(self.cwd), [])
        self.assertFalse((self.cwd / DATABASE_FILENAME).exists())
        self.assertFalse((REPO_ROOT / DATABASE_FILENAME).exists())


class EmptyAndWhitespacePathBoundaryTests(_CwdSandbox):
    def test_blank_strings_are_valueerror_on_every_guarded_helper(self) -> None:
        helpers = (
            ("open_database", open_database),
            ("database_path", database_path),
            ("database_path_for_legacy_file", database_path_for_legacy_file),
        )
        for blank in BLANK_STRINGS:
            for label, helper in helpers:
                with self.subTest(path=repr(blank), helper=label):
                    with self._spy_sqlite_connect() as targets:
                        with self.assertRaises(ValueError):
                            helper(blank)
                    self._assert_clean(targets)

    def test_blank_pathlike_is_rejected_before_sqlite(self) -> None:
        for blank in ("", "   ", "\t\n"):
            value = _FsPath(blank)
            self.assertIsInstance(value, os.PathLike)
            with self.subTest(fspath=repr(blank)):
                with self._spy_sqlite_connect() as targets:
                    with self.assertRaises(ValueError):
                        open_database(value)
                    with self.assertRaises(ValueError):
                        database_path_for_legacy_file(value)
                    with self.assertRaises(ValueError):
                        database_path(value)
                self._assert_clean(targets)

    def test_path_empty_and_dot_never_reach_sqlite_from_open_database(self) -> None:
        for bad in (Path(""), Path("."), ".", "./", Path("./")):
            with self.subTest(path=repr(bad)):
                with self._spy_sqlite_connect() as targets:
                    with self.assertRaises(ValueError):
                        open_database(bad)
                self._assert_clean(targets)

    def test_wrappers_reject_empty_database_path_without_legacy_import(self) -> None:
        legacy = self.cwd / "credentials.json"
        legacy.write_text('{"username": "keep-me"}', encoding="utf-8")
        before = legacy.read_text(encoding="utf-8")
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(ValueError):
                load_payload("", "credentials", {"fallback": True}, legacy_path=str(legacy))
            with self.assertRaises(ValueError):
                save_payload("", "credentials", {"username": "nope"})
            with self.assertRaises(ValueError):
                append_event("", "credentials", {"event": 1})
            with self.assertRaises(ValueError):
                load_events("", "credentials", legacy_path=str(legacy))
        self.assertEqual(targets, [], msg=f"sqlite3.connect was called with {targets!r}")
        self.assertEqual(_names(self.cwd), ["credentials.json"])
        self.assertTrue(legacy.is_file())
        self.assertEqual(legacy.read_text(encoding="utf-8"), before)
        self.assertFalse((self.cwd / DATABASE_FILENAME).exists())

    def test_non_path_scalars_remain_typeerror(self) -> None:
        for label, value in (("None", None), ("False", False), ("0", 0), ("empty-bytes", b"")):
            with self.subTest(value=label):
                with self._spy_sqlite_connect() as targets:
                    with self.assertRaises(TypeError):
                        open_database(value)  # type: ignore[arg-type]
                self._assert_clean(targets)

    def test_database_path_dot_still_derives_a_file_under_cwd(self) -> None:
        resolved = database_path(".")
        self.assertIsInstance(resolved, Path)
        self.assertEqual(resolved.name, DATABASE_FILENAME)
        self.assertTrue(str(resolved).endswith(str(Path("system") / "drone-app" / DATABASE_FILENAME)))
        self.assertEqual(resolved.parent, (self.cwd / "system" / "drone-app").resolve())
        self.assertFalse(resolved.exists())

    def test_rejected_blank_path_does_not_poison_a_later_open(self) -> None:
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(ValueError):
                open_database("")
            with self.assertRaises(ValueError):
                open_database("   ")
            path = str(self.cwd / "after" / "state.sqlite3")
            open_database(path).close()
        self.assertTrue(Path(path).is_file())
        self.assertTrue(any(Path(target).resolve() == Path(path).resolve() for target in targets))
        self.assertNotIn("", targets)
        self.assertNotIn(".", targets)
        self.assertFalse((self.cwd / DATABASE_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
