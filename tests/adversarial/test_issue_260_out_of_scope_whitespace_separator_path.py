"""Out-of-scope evidence for issue #260 (owned by the #220 empty/whitespace contract).

``open_database`` checks ``str.strip()`` on the raw string, so a whitespace-only final name hidden
behind a separator (``'sub/ '``, ``'\\u00a0/'``) looks like a directory path with visible text.
``Path`` then names a file made only of whitespace, and SQLite creates it. The #220 contract
treats whitespace-only names as blank. This is a separate contract from the format-only
characters that #260 covers, so this suite must not block #260 delivery.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage import state_store


class WhitespaceSeparatorPathOutOfScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sub").mkdir()
        self.original_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.original_cwd)
        os.chdir(self.root)
        env = mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_whitespace_only_name_behind_a_separator_is_rejected_before_connect(self):
        for value in ("sub/ ", "\u00a0/", "sub/\t/", " /"):
            with self.subTest(value=ascii(value)):
                with mock.patch.object(state_store.sqlite3, "connect") as connect:
                    with self.assertRaises(ValueError):
                        state_store.open_database(value)
                    connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
