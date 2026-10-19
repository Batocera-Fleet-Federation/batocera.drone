"""Adversarial boundary checks for issue #382: invisible names that are not format characters.

Issue #382 (follow-up to #260): the characters below render as blank but are not Unicode format
(Cf) characters. They are Hangul fillers and the Braille pattern blank (letter/symbol categories),
and combining or enclosing marks (Mn/Me) such as the combining grapheme joiner and variation
selectors. A name made only of them is blank. ``open_database`` must reject it before
``sqlite3.connect``, so no invisible file (or -wal/-shm) is created in the working directory.

This suite mocks ``sqlite3.connect`` so a failing run creates no invisible files on disk.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage import state_store


INVISIBLE_NON_FORMAT = (
    "ㅤ",  # HANGUL FILLER (Lo)
    "⠀",  # BRAILLE PATTERN BLANK (So)
    "ᅟ",  # HANGUL CHOSEONG FILLER (Lo)
    "ﾠ",  # HALFWIDTH HANGUL FILLER (Lo)
    "͏",  # COMBINING GRAPHEME JOINER (Mn)
    "️",  # VARIATION SELECTOR-16 (Mn), invisible with no base character
)


class InvisibleNonFormatNameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.original_cwd)
        os.chdir(self.root)
        env = mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_blank_looking_non_format_name_is_rejected_before_connect(self):
        for char in INVISIBLE_NON_FORMAT:
            with self.subTest(char=ascii(char)):
                with mock.patch.object(state_store.sqlite3, "connect") as connect:
                    with self.assertRaises(ValueError):
                        state_store.open_database(char)
                    connect.assert_not_called()

    def test_blank_non_format_names_are_rejected_at_every_entry_point(self):
        for char in INVISIBLE_NON_FORMAT:
            with self.subTest(char=ascii(char)):
                with mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": char}):
                    with self.assertRaises(ValueError):
                        state_store.database_path(self.root)
                    with self.assertRaises(ValueError):
                        state_store.database_path_for_legacy_file(self.root / "legacy.json")
                with mock.patch.object(state_store.sqlite3, "connect") as connect:
                    with self.assertRaises(ValueError):
                        state_store.open_database(self.root / "sub" / char)
                    connect.assert_not_called()
                self.assertFalse((self.root / "sub").exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_visible_name_containing_non_format_blank_characters_opens(self):
        for char in INVISIBLE_NON_FORMAT:
            name = "a" + char + "b.sqlite3"
            target = self.root / name
            with self.subTest(char=ascii(char)):
                connection = state_store.open_database(target)
                connection.close()
                self.assertTrue(target.is_file())
                target.unlink()


if __name__ == "__main__":
    unittest.main()
