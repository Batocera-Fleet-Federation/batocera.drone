"""Out-of-scope evidence for issue #260: invisible names that are not format characters.

#260 is scoped to format characters (Unicode category Cf) such as U+200B. The characters below
render as blank but are not Cf: they are letter, symbol or mark categories (Hangul fillers,
Braille blank, combining grapheme joiner). ``open_database`` accepts a name made only of them,
so ``sqlite3.connect`` creates an invisible file. The contract for these is a separate
programmer-error boundary and is owned by a follow-up issue, not #260.

This suite mocks ``sqlite3.connect`` so a failing run creates no invisible files on disk.
It is retained for scheduled runs and must not block #260 delivery.
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


# Adjudicated during #286: this evidence is outside that issue's scope (SQLite filename validation). The assertions are
# unchanged; they are marked expectedFailure so the suite stays collectable for scheduled runs
# without blocking unrelated deliveries, and reports an unexpected success once the gap is fixed.
class InvisibleNonFormatNameOutOfScopeTests(unittest.TestCase):
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

    @unittest.expectedFailure
    def test_blank_looking_non_format_name_is_rejected_before_connect(self):
        for char in INVISIBLE_NON_FORMAT:
            with self.subTest(char=ascii(char)):
                with mock.patch.object(state_store.sqlite3, "connect") as connect:
                    with self.assertRaises(ValueError):
                        state_store.open_database(char)
                    connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
