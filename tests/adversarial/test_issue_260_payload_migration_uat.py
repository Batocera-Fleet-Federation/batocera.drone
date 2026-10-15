"""Issue #260 UAT: invalid database names cannot consume legacy state.

A rejected programmer-error path must fail before opening SQLite or modifying
migration input. Visible names containing U+200B must retain their exact identity:
stripping the character would silently merge two independent state databases.
These expectations extend the issue's rejection/valid-name boundary to real I/O.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage import state_store


class TextPath:
    def __init__(self, text):
        self.text = text

    def __fspath__(self):
        return self.text


class PayloadMigrationUAT(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="issue260-migration-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.legacy = self.root / "legacy.json"
        self.source = b'{"games": ["original"], "revision": 7}\n'
        self.legacy.write_bytes(self.source)

    def test_invalid_database_cannot_read_migrate_or_overwrite_legacy_state(self):
        values = ("\u200b", TextPath("\ufeff\u200b"),
                  Path("missing") / "\u200b", "missing/ \u200b\ufeff\t/.")
        for value in values:
            with self.subTest(value=getattr(value, "text", value)):
                with mock.patch.object(state_store.sqlite3, "connect") as connect:
                    with self.assertRaises(ValueError):
                        state_store.load_payload(value, "uat", {}, legacy_path=self.legacy)
                    with self.assertRaises(ValueError):
                        state_store.save_payload(value, "uat", {"replacement": True})
                    connect.assert_not_called()
                self.assertEqual(self.legacy.read_bytes(), self.source)
                self.assertEqual(set(self.root.iterdir()), {self.legacy})

    def test_successful_migration_after_rejection_persists_across_reopen(self):
        with self.assertRaises(ValueError):
            state_store.load_payload("\u200b", "uat", {}, legacy_path=self.legacy)
        target = self.root / "state\u200b.sqlite3"
        payload = {"games": ["original"], "revision": 7}
        self.assertEqual(state_store.load_payload(target, "uat", {}, legacy_path=self.legacy), payload)
        self.assertFalse(self.legacy.exists())
        self.assertEqual(state_store.load_payload(str(target), "uat", None), payload)
        state_store.save_payload(target, "uat", {"revision": 8})
        self.assertEqual(state_store.load_payload(TextPath(str(target)), "uat", None), {"revision": 8})

    def test_visible_format_name_and_plain_name_remain_independent(self):
        plain = self.root / "state.sqlite3"
        formatted = self.root / "state\u200b.sqlite3"
        state_store.save_payload(plain, "uat", {"database": "plain"})
        state_store.save_payload(formatted, "uat", {"database": "formatted"})
        self.assertEqual(state_store.load_payload(plain, "uat", None), {"database": "plain"})
        self.assertEqual(state_store.load_payload(formatted, "uat", None), {"database": "formatted"})
        self.assertTrue(plain.is_file())
        self.assertTrue(formatted.is_file())


if __name__ == "__main__":
    unittest.main()
