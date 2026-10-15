"""Adversarial acceptance for issue #259: legacy-file DB derivation must not use the cwd's parent.

Expected contract, derived from the issue before reading the patch:

- ``database_path_for_legacy_file`` receives the path of a legacy *file*. Any spelling that
  names no file (``''``, ``'.'``, ``'./'``, ``'..'``, ``'/'``, custom ``PathLike`` objects that
  stringify to those) must fail closed with ``ValueError`` instead of returning
  ``<cwd>.parent/rom_metadata_cache.sqlite3``.
- The rejection must hold from any working directory depth, and must not create a database
  or any other file in the cwd, its parent, or any ancestor.
- ``open_database`` rejects the same blank values, so the helper and the opener must agree.
- Over-rejection is also a failure: file names that merely *start* with dots, or are relative
  names in the cwd, must still derive the database beside the legacy file.

These tests use real SQLite and real files. Only the process environment is isolated.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage import state_store


class _FsPath:
    """A ``PathLike`` whose filesystem path is a fixed string."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __fspath__(self) -> str:
        return self._value


BLANK_FORMS = (
    "",
    "   ",
    Path(""),
    ".",
    "./",
    "./.",
    Path("."),
    "..",
    Path(".."),
    "x/..",
    "/",
    "/..",
    _FsPath(""),
    _FsPath("."),
)


class LegacyFileBoundaryAdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.previous_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.previous_cwd)

    def _enter(self, *parts: str) -> Path:
        directory = self.root.joinpath(*parts)
        directory.mkdir(parents=True, exist_ok=True)
        os.chdir(directory)
        return directory

    def _snapshot(self) -> set[Path]:
        """Every entry under the temp root, so a stray DB or dir anywhere shows up."""
        return {path for path in self.root.rglob("*")}

    def test_every_blank_form_is_rejected_at_depth_without_touching_disk(self) -> None:
        for depth in (("work",), ("a", "b", "c")):
            with self.subTest(depth=depth):
                cwd = self._enter(*depth)
                before = self._snapshot()
                for form in BLANK_FORMS:
                    with self.subTest(form=repr(form)):
                        with self.assertRaises(ValueError):
                            state_store.database_path_for_legacy_file(form)
                self.assertEqual(self._snapshot(), before)
                self.assertFalse(list(cwd.parent.glob(state_store.DATABASE_FILENAME)))
                self.assertFalse(list(cwd.glob(state_store.DATABASE_FILENAME)))

    def test_no_ancestor_of_the_cwd_receives_a_database_for_blank_forms(self) -> None:
        cwd = self._enter("a", "b", "c")
        for form in BLANK_FORMS:
            with self.subTest(form=repr(form)):
                with self.assertRaises(ValueError):
                    state_store.database_path_for_legacy_file(form)
        for ancestor in (cwd, cwd.parent, cwd.parent.parent, self.root):
            self.assertFalse(
                (ancestor / state_store.DATABASE_FILENAME).exists(),
                f"unexpected database under {ancestor}",
            )

    def test_open_database_and_helper_agree_on_blank_forms(self) -> None:
        # The helper must refuse the same blank values the opener refuses. Only the forms that
        # open_database already rejects as ValueError are used: '/' and '..' reach sqlite and
        # chmod on the real filesystem through the opener (tracked separately as #220 OOS), so
        # they are never handed to open_database here.
        cwd = self._enter("work")
        opener_blank_forms = ("", "   ", Path(""), ".", "./", "./.", Path("."), _FsPath(""), _FsPath("."))
        for form in opener_blank_forms:
            with self.subTest(form=repr(form)):
                with self.assertRaises(ValueError):
                    state_store.database_path_for_legacy_file(form)
                with self.assertRaises(ValueError):
                    state_store.open_database(form)
        self.assertFalse((cwd.parent / state_store.DATABASE_FILENAME).exists())
        self.assertFalse((cwd / state_store.DATABASE_FILENAME).exists())

    def test_composed_open_of_blank_legacy_path_creates_no_sqlite_file(self) -> None:
        # Real composition: the helper is fed straight into the opener, the way a caller would.
        cwd = self._enter("work")
        with self.assertRaises(ValueError):
            state_store.open_database(state_store.database_path_for_legacy_file(Path("")))
        self.assertFalse((cwd.parent / state_store.DATABASE_FILENAME).exists())
        self.assertFalse((cwd / state_store.DATABASE_FILENAME).exists())

    def test_relative_file_names_in_cwd_derive_database_in_cwd_not_its_parent(self) -> None:
        # Positive control: the contrast with the blank case. A real file name in the cwd
        # must land beside that file, i.e. in the cwd itself.
        cwd = self._enter("work")
        for name in ("legacy.json", ".hidden", "..json", "...", "a.b.c", "name with space.json"):
            with self.subTest(name=name):
                resolved = state_store.database_path_for_legacy_file(name)
                self.assertEqual(resolved, cwd.resolve() / state_store.DATABASE_FILENAME)
                self.assertNotEqual(resolved.parent, cwd.parent.resolve())

    def test_nested_file_names_derive_their_own_directory_at_depth(self) -> None:
        cwd = self._enter("a", "b", "c")
        resolved = state_store.database_path_for_legacy_file(Path("../sibling/cred.json"))
        self.assertEqual(resolved, (cwd.parent / "sibling").resolve() / state_store.DATABASE_FILENAME)

    def test_legacy_file_derivation_is_usable_by_the_opener(self) -> None:
        # The accepted path must be openable and writable by real SQLite, so the fix does not
        # trade the bug for a path that can never be used.
        cwd = self._enter("work")
        database = state_store.database_path_for_legacy_file("legacy.json")
        connection = state_store.open_database(database)
        try:
            connection.execute("SELECT 1 FROM app_state").fetchall()
        finally:
            connection.close()
        self.assertTrue((cwd / state_store.DATABASE_FILENAME).is_file())
        with sqlite3.connect(str(database)) as check:
            self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_credential_store_without_explicit_database_fails_closed_in_cwd_tree(self) -> None:
        # The credential store derives its DB from the credentials path when none is injected.
        # With a blank credentials path it must raise and leave no database in the parent.
        from app.common import auth

        cwd = self._enter("work")
        with self.assertRaises(ValueError):
            auth.DroneCredentialStore(Path(""))
        self.assertFalse((cwd.parent / state_store.DATABASE_FILENAME).exists())


if __name__ == "__main__":
    unittest.main()
