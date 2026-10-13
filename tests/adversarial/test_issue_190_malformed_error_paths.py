"""Adversarial malformed-input and error-path checks for issue #190.

Issue #190 is a type-contract hole: the guard accepts ``str`` / ``os.PathLike``,
then the helpers assumed ``pathlib.Path`` and raised ``AttributeError``.

Expected behavior, derived from the issue and the existing mock-guard contract:

- Values that are neither ``str`` nor ``os.PathLike`` raise ``TypeError``
  (``must be a filesystem path``) before SQLite opens, never ``AttributeError``.
- A PathLike whose ``__fspath__`` returns bytes is not a text filesystem path;
  ``TypeError`` before SQLite, no files in cwd (pinned by #170, re-checked so
  a ``Path()`` wrap cannot silently open a bytes path).
- A PathLike whose ``__fspath__`` raises propagates that error before SQLite.
- When the parent of a str path exists as a file, the failure is an ``OSError``
  from mkdir/connect, not ``AttributeError``.
- Unittest mocks remain rejected even though they are PathLike (issue #170).
  This suite only samples that pin; the #170 suites remain authoritative.

Cases run from an empty temporary cwd so stray connect targets are visible.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage.state_store import database_path_for_legacy_file, open_database, save_payload


REPO_ROOT = Path(__file__).resolve().parents[2]
REJECTION = "must be a filesystem path"


class _FsPath:
    def __init__(self, value) -> None:
        self._value = value

    def __fspath__(self):
        return self._value


class _RaisingFsPath:
    def __fspath__(self):
        raise OSError("fspath unavailable")


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

    def _assert_clean(self, targets: list[str]) -> None:
        self.assertEqual(targets, [], msg=f"sqlite3.connect was called with {targets!r}")
        self.assertEqual(_mock_named(self.cwd), [])
        self.assertEqual(_mock_named(REPO_ROOT), [])
        self.assertEqual(sorted(path.name for path in self.cwd.iterdir()), [])


class NonPathRejectionTests(_CwdSandbox):
    def test_non_path_values_raise_typeerror_not_attributeerror(self) -> None:
        values = {
            "None": None,
            "int": 12345,
            "float": 1.5,
            "bool": True,
            "list": [],
            "dict": {},
            "object": object(),
            "bytes": b"/tmp/x.sqlite3",
            "bytearray": bytearray(b"/tmp/x.sqlite3"),
            "memoryview": memoryview(b"/tmp/x.sqlite3"),
        }
        for label, value in values.items():
            with self.subTest(value=label, helper="open_database"):
                with self._spy_sqlite_connect() as targets:
                    with self.assertRaises(TypeError) as raised:
                        open_database(value)  # type: ignore[arg-type]
                    self.assertNotIsInstance(raised.exception, AttributeError)
                    self.assertRegex(str(raised.exception), REJECTION)
                self._assert_clean(targets)
            with self.subTest(value=label, helper="database_path_for_legacy_file"):
                with self._spy_sqlite_connect() as targets:
                    with self.assertRaises(TypeError) as raised:
                        database_path_for_legacy_file(value)  # type: ignore[arg-type]
                    self.assertNotIsInstance(raised.exception, AttributeError)
                    self.assertRegex(str(raised.exception), REJECTION)
                self._assert_clean(targets)

    def test_bytes_pathlike_is_typeerror_before_sqlite(self) -> None:
        value = _FsPath(b"/tmp/issue-190-bytes.sqlite3")
        self.assertIsInstance(value, os.PathLike)
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(TypeError) as opened:
                open_database(value)
            with self.assertRaises(TypeError) as legacy:
                database_path_for_legacy_file(value)
            self.assertNotIsInstance(opened.exception, AttributeError)
            self.assertNotIsInstance(legacy.exception, AttributeError)
        self._assert_clean(targets)

    def test_pathlike_fspath_error_propagates_before_sqlite(self) -> None:
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(OSError):
                open_database(_RaisingFsPath())
            with self.assertRaises(OSError):
                database_path_for_legacy_file(_RaisingFsPath())
        self._assert_clean(targets)

    def test_spec_str_mock_is_still_rejected(self) -> None:
        # Regression pin: widening str acceptance must not re-open spec=str mocks.
        with self._spy_sqlite_connect() as targets:
            with self.assertRaisesRegex(TypeError, REJECTION):
                open_database(mock.MagicMock(spec=str))
            with self.assertRaisesRegex(TypeError, REJECTION):
                database_path_for_legacy_file(mock.MagicMock(spec=str))
            with self.assertRaisesRegex(TypeError, REJECTION):
                save_payload(mock.MagicMock(spec=str), "issue_190", {})
        self._assert_clean(targets)


class FilesystemErrorPathTests(_CwdSandbox):
    def test_str_path_whose_parent_is_a_file_raises_oserror_not_attributeerror(self) -> None:
        blocked = self.cwd / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        target = str(blocked / "child.sqlite3")
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(OSError) as raised:
                open_database(target)
            self.assertNotIsInstance(raised.exception, AttributeError)
        self.assertEqual(targets, [])
        self.assertFalse((blocked / "child.sqlite3").exists())
        self.assertEqual(blocked.read_text(encoding="utf-8"), "not a directory")

    def test_rejected_value_does_not_poison_a_later_str_open(self) -> None:
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(TypeError):
                open_database(None)  # type: ignore[arg-type]
            path = str(self.cwd / "after" / "state.sqlite3")
            open_database(path).close()
        self.assertTrue(Path(path).is_file())
        self.assertTrue(any(Path(target).resolve() == Path(path).resolve() for target in targets))
        self.assertEqual(_mock_named(self.cwd), [])
        self.assertEqual(_mock_named(REPO_ROOT), [])


if __name__ == "__main__":
    unittest.main()
