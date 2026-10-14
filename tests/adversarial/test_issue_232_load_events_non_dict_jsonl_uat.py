"""Adversarial UAT for issue #232: load_events must not unlink JSONL with no dicts.

Issue #232: ``load_events`` json.loads every non-empty JSONL line, inserts only
``isinstance(entry, dict)`` rows, then unlinked the file whenever the parsed
list was truthy. A file of JSON numbers/strings therefore disappeared with no
rows imported.

Reproduction from the issue: write ``42\\n"x"\\n`` and call
``load_events(str(db), 'ns', legacy_path=str(jsonl))``. Result is ``[]``; the
file must remain so an operator can inspect or rewrite it.

Expected behavior, derived from that contract and the one-time JSONL import
invariant (SQLite is the system of record only after a real event row exists):

- A JSONL of non-object JSON (numbers, strings, ...) returns ``[]``, leaves the
  file in place with the original bytes, and writes no ``app_events`` rows.
- A later load of the same file is idempotent: still ``[]``, still on disk.
- After the operator rewrites the file to object JSONL, the next load imports
  those dicts, unlinks the file, and a subsequent load without the file still
  returns the migrated events.
- A JSONL of dicts, including an empty object ``{}``, still migrates and unlinks
  (empty dict is a dict; the miss is "no dict entries", not "falsy payload").
- Valid multi-object JSONL still returns newest-first and does not regress the
  #207 str-path migration.

Cases run from an empty temporary cwd with a cleared environment.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage.state_store import load_events


REPRO_JSONL = '42\n"x"\n'


class _CwdSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmpdir.name).resolve()
        os.chdir(self.cwd)

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._tmpdir.cleanup()
        self._env.stop()

    def _event_count(self, database: str, namespace: str) -> int:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM app_events WHERE namespace = ?",
                (namespace,),
            ).fetchone()
        return int(row[0])


class Issue232ReportedReproductionTests(_CwdSandbox):
    def test_number_and_string_jsonl_returns_empty_and_keeps_file(self) -> None:
        # Shape of the reported repro: write 42\n"x"\n and call load_events
        # with str paths.
        legacy = self.cwd / "events.jsonl"
        legacy.write_text(REPRO_JSONL, encoding="utf-8")
        database = str(self.cwd / "e.sqlite3")
        events = load_events(database, "ns", legacy_path=str(legacy))
        self.assertEqual(events, [])
        self.assertTrue(legacy.is_file())
        self.assertEqual(legacy.read_text(encoding="utf-8"), REPRO_JSONL)
        self.assertEqual(self._event_count(database, "ns"), 0)

    def test_kept_non_dict_jsonl_is_idempotent_across_a_second_load(self) -> None:
        legacy = self.cwd / "again.jsonl"
        legacy.write_text(REPRO_JSONL, encoding="utf-8")
        database = str(self.cwd / "again.sqlite3")
        first = load_events(database, "ns", legacy_path=str(legacy))
        second = load_events(database, "ns", legacy_path=str(legacy))
        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertTrue(legacy.is_file())
        self.assertEqual(legacy.read_text(encoding="utf-8"), REPRO_JSONL)
        self.assertEqual(self._event_count(database, "ns"), 0)

    def test_operator_can_rewrite_kept_file_and_retry_import(self) -> None:
        legacy = self.cwd / "retry.jsonl"
        legacy.write_text(REPRO_JSONL, encoding="utf-8")
        database = str(self.cwd / "retry.sqlite3")
        first = load_events(database, "ns", legacy_path=str(legacy))
        self.assertEqual(first, [])
        self.assertTrue(legacy.is_file())

        legacy.write_text('{"migrated": true}\n', encoding="utf-8")
        retried = load_events(database, "ns", legacy_path=str(legacy))
        self.assertEqual(retried, [{"migrated": True}])
        self.assertFalse(legacy.exists())
        self.assertEqual(self._event_count(database, "ns"), 1)

        again = load_events(database, "ns")
        self.assertEqual(again, [{"migrated": True}])


class Issue232DictJsonlStillMigratesTests(_CwdSandbox):
    def test_object_jsonl_still_imports_and_unlinks(self) -> None:
        legacy = self.cwd / "ok.jsonl"
        legacy.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
        database = str(self.cwd / "ok.sqlite3")
        events = load_events(database, "ns", legacy_path=str(legacy))
        self.assertEqual(events, [{"b": 2}, {"a": 1}])
        self.assertFalse(legacy.exists())
        self.assertEqual(load_events(database, "ns"), [{"b": 2}, {"a": 1}])

    def test_empty_object_is_a_dict_and_is_imported(self) -> None:
        # ``{}`` is a JSON object. The bug is "no dicts imported", not "falsy
        # event payload". A file of empty objects must migrate and unlink.
        legacy = self.cwd / "empty-obj.jsonl"
        legacy.write_text("{}\n", encoding="utf-8")
        database = str(self.cwd / "empty-obj.sqlite3")
        events = load_events(database, "ns", legacy_path=str(legacy))
        self.assertEqual(events, [{}])
        self.assertFalse(legacy.exists())
        self.assertEqual(self._event_count(database, "ns"), 1)


if __name__ == "__main__":
    unittest.main()
