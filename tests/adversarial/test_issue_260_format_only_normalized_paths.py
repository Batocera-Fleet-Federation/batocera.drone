"""Adversarial path-boundary checks for issue #260: format-only names hidden by normalization.

A database path whose *final name* has no visible character (only Unicode format characters
such as U+200B) has no usable filename, so it must be rejected before ``sqlite3.connect``.
``Path`` normalization can hide that name behind separators and ``.`` components:
``'​/'``, ``'​/.'``, ``'sub/​'`` and ``'./​'`` all name a file called U+200B.
Those forms must be rejected too. Visible text beside a format character stays a valid name.
"""

from __future__ import annotations

import os
import sqlite3
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


class _PathLike:
    def __init__(self, value):
        self.value = value

    def __fspath__(self):
        return self.value


class FormatOnlyNormalizedPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.original_cwd)
        os.chdir(self.root)
        (self.root / "sub").mkdir()
        env = mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": ""})
        env.start()
        self.addCleanup(env.stop)

    def _assert_rejected_before_connect(self, value):
        with mock.patch.object(state_store.sqlite3, "connect") as connect:
            with self.assertRaises(ValueError):
                state_store.open_database(value)
            connect.assert_not_called()

    def test_separator_forms_that_name_a_format_only_file_are_rejected_before_connect(self):
        values = (
            ZWSP + "/",
            ZWSP + "//",
            ZWSP + "/.",
            "./" + ZWSP,
            "./" + ZWSP + "/",
            BOM + "/",
            WORD_JOINER + ZWSP + "/",
            "sub/" + ZWSP,
            "sub/" + ZWSP + "/",
            "sub/" + BOM + "/.",
            ZWSP + "/" + ZWSP,
        )
        for value in values:
            with self.subTest(value=ascii(value)):
                self._assert_rejected_before_connect(value)

    def test_pathlike_and_utf8_bytes_forms_are_rejected_before_connect(self):
        self._assert_rejected_before_connect(_PathLike(ZWSP + "/"))
        self._assert_rejected_before_connect(_PathLike((ZWSP + "/").encode("utf-8")))
        self._assert_rejected_before_connect(_PathLike(("sub/" + BOM).encode("utf-8")))

    def test_path_objects_that_normalize_to_format_only_names_are_rejected(self):
        for value in (Path(ZWSP + "/"), Path("./" + ZWSP), Path("sub") / ZWSP):
            with self.subTest(value=ascii(str(value))):
                self._assert_rejected_before_connect(value)

    def test_real_connect_leaves_no_invisible_artifacts_behind(self):
        for value in (ZWSP + "/", ZWSP + "/.", "./" + ZWSP, "sub/" + ZWSP):
            with self.subTest(value=ascii(value)):
                with self.assertRaises(ValueError):
                    state_store.open_database(value)
                self.assertEqual(sorted(os.listdir(self.root)), ["sub"])
                self.assertEqual(os.listdir(self.root / "sub"), [])

    def test_visible_names_beside_format_characters_still_open(self):
        targets = (
            ZWSP + "state.sqlite3",
            "state" + ZWSP,
            "state" + BOM + "/",
            "sub/" + ZWSP + "x",
            "sub/a" + WORD_JOINER + "b.sqlite3/",
        )
        for value in targets:
            with self.subTest(value=ascii(value)):
                connection = state_store.open_database(value)
                connection.close()
                target = Path(value)
                self.assertTrue(target.is_file(), target)
                self.assertTrue(_has_visible_character(target.name))

    def test_format_character_directory_with_visible_file_name_is_accepted(self):
        target = self.root / (ZWSP + "dir") / "state.sqlite3"
        connection = state_store.open_database(target)
        connection.close()
        self.assertTrue(target.is_file())

    def test_non_format_space_names_still_rejected_at_the_plain_entry_point(self):
        # NBSP and ideographic space are rejected by strip(); keep that boundary intact.
        for value in (" ", "　", " " + ZWSP + " "):
            with self.subTest(value=ascii(value)):
                self._assert_rejected_before_connect(value)

    def test_connect_spy_positive_control_reaches_sqlite_for_visible_name(self):
        target = self.root / ("ok" + ZWSP + ".sqlite3")
        with mock.patch.object(state_store.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            connection = state_store.open_database(target)
            connection.close()
        connect.assert_called_once()
        self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
