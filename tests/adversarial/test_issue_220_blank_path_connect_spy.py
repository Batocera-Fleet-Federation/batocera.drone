"""Adversarial acceptance for issue #220: blank database paths never call ``sqlite3.connect``.

Issue #220: ``open_database('')`` reached ``sqlite3.connect('.')``. The contract derived
from the issue, before reading the patch:

- Every blank form (empty ``str``, ``Path('')``, ``Path('.')``, ``'.'``, ``'./'``, and
  ``os.PathLike`` values whose ``__fspath__`` is ``''`` or ``b''``) is a ``ValueError``
  from ``open_database``, and ``sqlite3.connect`` is never called with it.
- ``DRONE_STATE_DATABASE_FILE`` is not consulted by ``open_database``, so a valid override
  cannot rescue or redirect an explicitly blank argument.
- The legacy and root helpers reject blank input as ``ValueError`` and never return a
  directory-shaped database path.
- Positive controls: a real path still connects exactly once with that exact string, and
  names with leading or inner spaces are opened verbatim, not stripped.

Each case runs from an empty temporary cwd so stray connect targets are observable.
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
    database_path,
    database_path_for_legacy_file,
    open_database,
)


BLANK_PATHS = ("", Path(""), Path("."), ".", "./", Path("./"))


class _EmptyPathLike:
    def __fspath__(self) -> str:
        return ""


class _EmptyBytesPathLike:
    def __fspath__(self) -> bytes:
        return b""


class BlankPathConnectSpyTests(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict("os.environ")
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DRONE_STATE_DATABASE_FILE", None)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self._cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)

    def _entries(self) -> set[str]:
        return set(os.listdir(self.root))

    @contextlib.contextmanager
    def _connect_spy(self):
        with mock.patch("sqlite3.connect") as connect:
            yield connect

    def test_open_database_rejects_every_blank_form_before_connect(self) -> None:
        blanks = [*BLANK_PATHS, _EmptyPathLike(), _EmptyBytesPathLike()]
        for blank in blanks:
            with self.subTest(blank=blank):
                before = self._entries()
                with self._connect_spy() as connect:
                    with self.assertRaises(ValueError):
                        open_database(blank)
                connect.assert_not_called()
                self.assertEqual(self._entries(), before)

    def test_valid_database_file_override_does_not_rescue_a_blank_argument(self) -> None:
        configured = self.root / "configured.sqlite3"
        os.environ["DRONE_STATE_DATABASE_FILE"] = str(configured)
        with self._connect_spy() as connect:
            with self.assertRaises(ValueError):
                open_database("")
        connect.assert_not_called()
        self.assertFalse(configured.exists())

    def test_legacy_and_root_helpers_reject_empty_string_as_value_error(self) -> None:
        # Path('') is deliberately not asserted here: it equals Path('.'), which is a
        # legitimate userdata root for database_path(), so the two cannot be told apart.
        for blank in ("", _EmptyPathLike()):
            with self.subTest(blank=blank):
                with self.assertRaises(ValueError):
                    database_path_for_legacy_file(blank)
        with self.assertRaises(ValueError):
            database_path("")

    def test_real_path_connects_exactly_once_with_that_string(self) -> None:
        target = self.root / "real.sqlite3"
        with mock.patch("sqlite3.connect", wraps=sqlite3.connect) as connect:
            open_database(target).close()
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(connect.call_args.args[0], str(target))
        self.assertTrue(target.is_file())

    def test_names_with_inner_and_leading_spaces_are_opened_verbatim(self) -> None:
        for name in (" lead.sqlite3", "inner space.sqlite3"):
            with self.subTest(name=name):
                open_database(self.root / name).close()
                self.assertIn(name, self._entries())


if __name__ == "__main__":
    unittest.main()
