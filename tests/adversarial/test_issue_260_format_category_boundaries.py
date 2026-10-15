"""Adversarial boundary checks for issue #260 across the full Unicode format (Cf) category.

Issue #260: a database path whose visible name is only invisible format text (U+200B) must never
reach ``sqlite3.connect``. The contract is the category, not one code point: soft hyphen, the
directional marks and overrides, the Mongolian vowel separator, the Arabic letter mark, the
interlinear anchors and tag characters are all format characters with no visible glyph. Each must
be rejected before SQLite is called, from every entry point, in every wrapping form.

Expectations are derived from the issue, not from the implementation:

- A name whose final component is only format characters (optionally with whitespace) is a
  ``ValueError`` before ``sqlite3.connect``, from ``open_database``, the env override of
  ``database_path`` and ``database_path_for_legacy_file``.
- Rejection happens before the parent directory is created, so a rejected path leaves no trace.
- A visible name that merely contains a format character opens once, with that exact name.
- Positive control: a visible name with a non-breaking space (not format, not stripped) stays verbatim.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage import state_store


# Explicit escapes keep the invisible code points visible to reviewers of this file.
FORMAT_CHARACTERS = (
    "­",  # SOFT HYPHEN
    "​",  # ZERO WIDTH SPACE
    "‌",  # ZERO WIDTH NON-JOINER
    "‎",  # LEFT-TO-RIGHT MARK
    "‏",  # RIGHT-TO-LEFT MARK
    "‮",  # RIGHT-TO-LEFT OVERRIDE
    "⁦",  # LEFT-TO-RIGHT ISOLATE
    "⁩",  # POP DIRECTIONAL ISOLATE
    "؜",  # ARABIC LETTER MARK
    "᠎",  # MONGOLIAN VOWEL SEPARATOR
    "￹",  # INTERLINEAR ANNOTATION ANCHOR
    "\U000e0001",  # LANGUAGE TAG
)


class _PathLike:
    def __init__(self, value):
        self.value = value

    def __fspath__(self):
        return self.value


class FormatCategoryBoundaryTests(unittest.TestCase):
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

    def test_each_format_character_alone_is_rejected_at_every_entry_point(self):
        for char in FORMAT_CHARACTERS:
            forms = (
                char,
                char + "/",
                "sub/" + char,
                "sub/" + char + "/.",
                " " + char + " ",
                _PathLike(char),
                _PathLike(char.encode("utf-8")),
            )
            for value in forms:
                with self.subTest(char=ascii(char), value=ascii(getattr(value, "value", value))):
                    with mock.patch.object(state_store.sqlite3, "connect") as connect:
                        with self.assertRaises(ValueError):
                            state_store.open_database(value)
                        connect.assert_not_called()
                    self.assertEqual(sorted(os.listdir(self.root)), ["sub"])
                    self.assertEqual(os.listdir(self.root / "sub"), [])

    def test_env_override_and_legacy_helper_reject_each_format_only_name(self):
        for char in FORMAT_CHARACTERS:
            with self.subTest(char=ascii(char)):
                with mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": char}):
                    with self.assertRaises(ValueError):
                        state_store.database_path(self.root)
                    with self.assertRaises(ValueError):
                        state_store.database_path_for_legacy_file(self.root / "legacy.json")
                with mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": "sub/" + char + "/"}):
                    with self.assertRaises(ValueError):
                        state_store.database_path(self.root)

    def test_rejection_happens_before_parent_directory_is_created(self):
        for char in FORMAT_CHARACTERS:
            with self.subTest(char=ascii(char)):
                with mock.patch.object(state_store.sqlite3, "connect") as connect:
                    with self.assertRaises(ValueError):
                        state_store.open_database(self.root / "newdir" / char)
                    connect.assert_not_called()
                self.assertFalse((self.root / "newdir").exists())

    def test_visible_name_containing_each_format_character_connects_once_with_exact_name(self):
        for char in FORMAT_CHARACTERS:
            name = "a" + char + "b.sqlite3"
            target = self.root / name
            with self.subTest(char=ascii(char)):
                with mock.patch.object(state_store.sqlite3, "connect", wraps=sqlite3.connect) as connect:
                    connection = state_store.open_database(target)
                    connection.close()
                connect.assert_called_once()
                self.assertEqual(connect.call_args.args[0], str(target))
                self.assertTrue(target.is_file())

    def test_non_breaking_space_inside_a_visible_name_is_opened_verbatim(self):
        target = self.root / "sp ace.sqlite3"
        with mock.patch.object(state_store.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            connection = state_store.open_database(target)
            connection.close()
        connect.assert_called_once()
        self.assertEqual(connect.call_args.args[0], str(target))
        self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
