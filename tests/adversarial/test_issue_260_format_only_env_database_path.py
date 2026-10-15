"""Adversarial override checks for issue #260: a format-only DRONE_STATE_DATABASE_FILE.

``database_path()`` returns the env override resolved to an absolute path without running the
blank-path guard on the raw value, so ``open_database`` would see ``/cwd/<U+200B>`` and accept
it. The contract is that no ``sqlite3.connect`` call may target a file whose name has no visible
character. Rejecting the override or falling back to the userdata default are both acceptable.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import unicodedata
from pathlib import Path
from unittest import mock

from app.storage import state_store


ZWSP = "​"
BOM = "﻿"
WORD_JOINER = "⁠"


def _has_visible_character(name: str) -> bool:
    return any(not char.isspace() and unicodedata.category(char) != "Cf" for char in name)


class FormatOnlyEnvDatabasePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.userdata = self.root / "userdata"
        self.userdata.mkdir()
        self.original_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.original_cwd)
        os.chdir(self.root)

    def _open_with_override(self, value):
        with mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": value}):
            return state_store.open_database(state_store.database_path(self.userdata))

    def test_format_only_override_never_reaches_sqlite_with_an_invisible_name(self):
        for value in (ZWSP, ZWSP + "/", ZWSP + "/.", BOM, WORD_JOINER + ZWSP):
            with self.subTest(value=ascii(value)):
                with mock.patch.object(state_store.sqlite3, "connect") as connect:
                    try:
                        connection = self._open_with_override(value)
                    except ValueError:
                        connection = None
                    if connection is not None:
                        connection.close()
                for call in connect.call_args_list:
                    connected = Path(call.args[0])
                    self.assertTrue(
                        _has_visible_character(connected.name),
                        f"sqlite3.connect received invisible name {ascii(connected.name)}",
                    )

    def test_format_only_override_leaves_no_invisible_files_in_cwd(self):
        for value in (ZWSP, ZWSP + "/"):
            with self.subTest(value=ascii(value)):
                try:
                    self._open_with_override(value).close()
                except ValueError:
                    pass
                self.assertFalse(
                    any(not _has_visible_character(name) for name in os.listdir(self.root)),
                    os.listdir(self.root),
                )

    def test_visible_override_name_with_format_character_still_opens(self):
        override = "state" + ZWSP + ".sqlite3"
        connection = self._open_with_override(override)
        connection.close()
        self.assertTrue((self.root / override).is_file())


if __name__ == "__main__":
    unittest.main()
