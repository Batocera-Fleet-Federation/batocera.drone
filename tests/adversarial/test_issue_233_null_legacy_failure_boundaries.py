"""Failure-path boundaries for issue #233's JSON null legacy migration.

Expected, from the issue and the import invariant:

- A legacy file is unlinked only after its document is durably stored. If the
  store write fails, the file must survive so the next start can retry.
- A miss (absent, unreadable, directory, no path) returns the caller's own
  fallback object, identity included. The internal miss sentinel must never leak.
- Migration deletes only the path the caller named. A symlink named as the
  legacy path loses the link, and its target stays.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage import state_store
from app.storage.state_store import load_payload


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name).resolve()
        self.db = self.root / "state.sqlite3"
        self.legacy = self.root / "legacy.json"


class NullLegacySaveFailureTests(_Sandbox):
    def test_failed_store_write_keeps_null_legacy_file_for_retry(self) -> None:
        self.legacy.write_text("null", encoding="utf-8")
        fallback = {"fallback": True}

        with mock.patch.object(
            state_store, "save_payload", side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            try:
                result = load_payload(self.db, "ns", fallback, legacy_path=self.legacy)
            except sqlite3.OperationalError:
                result = "raised"

        # A failed persist must not present as a successful migration.
        self.assertTrue(result == "raised" or result is fallback, msg=repr(result))
        self.assertTrue(self.legacy.exists(), "legacy file deleted before its document was stored")
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), "null")

    def test_retry_after_failed_write_imports_null_and_unlinks(self) -> None:
        self.legacy.write_text("null", encoding="utf-8")

        with mock.patch.object(state_store, "save_payload", side_effect=sqlite3.OperationalError("locked")):
            try:
                load_payload(self.db, "ns", {"fallback": True}, legacy_path=self.legacy)
            except sqlite3.OperationalError:
                pass

        self.assertIsNone(load_payload(self.db, "ns", {"fallback": True}, legacy_path=self.legacy))
        self.assertFalse(self.legacy.exists())
        self.assertIsNone(load_payload(self.db, "ns", {"fallback": True}))


class MissIdentityTests(_Sandbox):
    def test_absent_unreadable_and_directory_misses_return_the_same_fallback_object(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        directory = self.root / "a-dir.json"
        directory.mkdir()
        cases = {
            "absent": self.root / "absent.json",
            "unreadable": bad,
            "directory": directory,
            "no-path": None,
        }
        for label, legacy in cases.items():
            with self.subTest(case=label):
                fallback = object()
                result = load_payload(self.db, "ns", fallback, legacy_path=legacy)
                self.assertIs(result, fallback)

    def test_miss_writes_no_row_so_a_later_fallback_still_applies(self) -> None:
        self.assertEqual(load_payload(self.db, "ns", "first", legacy_path=self.root / "absent.json"), "first")
        self.assertEqual(load_payload(self.db, "ns", "second"), "second")


class SymlinkedLegacyTests(_Sandbox):
    def test_symlinked_null_legacy_removes_only_the_link_and_keeps_target(self) -> None:
        target = self.root / "real-null.json"
        target.write_text("null", encoding="utf-8")
        try:
            os.symlink(target, self.legacy)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        self.assertIsNone(load_payload(self.db, "ns", {"fallback": True}, legacy_path=self.legacy))
        self.assertFalse(os.path.lexists(self.legacy))
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "null")


if __name__ == "__main__":
    unittest.main()
