"""Out-of-scope evidence for issue #260 (owned by the #220 empty/whitespace contract).

A whitespace-only ``DRONE_STATE_DATABASE_FILE`` is resolved by ``database_path()`` without the
blank-path guard, so ``open_database`` receives ``/cwd/<spaces>`` and SQLite creates a database
file named with spaces. The #220 contract treats whitespace-only paths as blank. This suite is
not part of the #260 format-only contract and must not block #260 delivery.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage import state_store


class WhitespaceEnvDatabasePathOutOfScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.userdata = self.root / "userdata"
        self.userdata.mkdir()
        self.original_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.original_cwd)
        os.chdir(self.root)

    def test_whitespace_only_override_never_reaches_sqlite_with_a_blank_name(self):
        for value in ("   ", " ", "\t"):
            with self.subTest(value=value):
                with mock.patch.object(state_store.sqlite3, "connect") as connect:
                    with mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": value}):
                        try:
                            state_store.open_database(state_store.database_path(self.userdata)).close()
                        except ValueError:
                            pass
                for call in connect.call_args_list:
                    self.assertTrue(
                        Path(call.args[0]).name.strip(),
                        f"sqlite3.connect received blank name {call.args[0]!r}",
                    )


if __name__ == "__main__":
    unittest.main()
