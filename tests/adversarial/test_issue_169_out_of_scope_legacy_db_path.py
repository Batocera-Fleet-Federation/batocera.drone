"""Out-of-scope adversarial finding for issue #169: the legacy DB helper has no mock guard.

Issue #169 is scoped to ``database_path()`` and its ``<root>/system/drone-app`` state DB.
``database_path_for_legacy_file(path)`` is a sibling helper that skips the mock guard
entirely. Given a spec'd mock it returns a mock-derived path, and ``open_database`` then
hands ``str(mock)`` to SQLite, creating a mock-named SQLite file in the working directory
(not ``system/drone-app/rom_metadata_cache.sqlite3``).

This is reported as out_of_scope, not a blocking requirement of #169, because:

- the artifact is a mock-named file, not the state DB under ``system/drone-app``;
- the #162 finding already scoped mock-named artifacts out of its delivery;
- the one production caller (``app/common/auth.py``) passes a real auth-file path.

The assertions here are the evidence. They are kept in their own suite so the worker can
file the finding separately without making it a blocking test for #169.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage.state_store import database_path_for_legacy_file, open_database


SPEC_ROOT_FACTORIES = {
    "MagicMock(spec=Path)": lambda: mock.MagicMock(spec=Path),
    "create_autospec(Path, instance=True)": lambda: mock.create_autospec(Path, instance=True),
}


class LegacyDatabasePathSpecMockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()

    @contextlib.contextmanager
    def _fresh_cwd(self):
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            os.chdir(cwd)
            try:
                yield cwd
            finally:
                os.chdir(previous)

    def test_legacy_helper_rejects_spec_path_mock_before_sqlite_opens(self) -> None:
        for label, factory in SPEC_ROOT_FACTORIES.items():
            with self.subTest(path=label), self._fresh_cwd() as cwd:
                real_connect = sqlite3.connect
                targets: list[str] = []

                def spy(database, *args, **kwargs):
                    targets.append(str(database))
                    return real_connect(database, *args, **kwargs)

                with mock.patch("app.storage.state_store.sqlite3.connect", side_effect=spy):
                    try:
                        open_database(database_path_for_legacy_file(factory())).close()
                    except TypeError:
                        pass  # Rejection is the expected outcome.

                self.assertEqual(
                    targets,
                    [],
                    msg="legacy helper handed a spec'd mock straight to sqlite3.connect",
                )
                self.assertEqual(sorted(p.name for p in cwd.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
