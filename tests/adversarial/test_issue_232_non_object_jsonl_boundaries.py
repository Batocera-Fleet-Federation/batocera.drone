"""Adversarial malformed-input and boundary checks for issue #232.

``load_events`` only persists JSON objects as event rows. Unlink is the
one-time-import side effect and is allowed only after at least one dict row
was actually inserted. A parsed JSONL whose lines are all non-objects is not
a successful import: the file must stay, bytes unchanged, and SQLite must
gain no ``app_events`` rows for that namespace.

Expected, from the issue (do not vanish a non-object JSONL with no rows) and
the JSONL-of-objects domain invariant:

- Every JSON value that is not an object (null, bool, number, string, array,
  NaN/Infinity) keeps the file and imports nothing, including the classic
  truthiness traps ``0``, ``false``, ``null``, and ``""``.
- A JSON array of objects written as one line is an array, not an event dict;
  keep the file so the operator can convert it to JSONL.
- Blank, whitespace-only, and empty files never count as an import.
- Mixed object + non-object JSONL imports the dicts and then unlinks: the
  issue unlinks once something was imported, not only when every line is a
  dict.
- A populated namespace still does not consume a non-object file.
- A second namespace with an empty event table must not steal a file that
  another namespace already imported.
- PathLike and str legacy paths follow the same keep/unlink rule.
- Parse failure of any line still fail-closes (no partial import, no unlink),
  matching the #207 truncated-JSONL contract.

Cases run from an empty temporary cwd so stray files are visible.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from app.storage.state_store import append_event, load_events


class _FsPath:
    """os.PathLike that is not pathlib.Path and has no exists/read_text/unlink."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __fspath__(self) -> str:
        return self._value


# Each value is a complete JSONL body. Python's json.loads accepts NaN/Infinity.
_NON_OBJECT_JSONL = {
    "zero": "0\n",
    "false": "false\n",
    "null": "null\n",
    "empty-string": '""\n',
    "empty-array": "[]\n",
    "number-and-string": '42\n"x"\n',
    "negative-float": "-1.5\n",
    "scientific": "1e10\n",
    "true": "true\n",
    "unicode-string": '"café"\n',
    "array-of-objects": '[{"a": 1}, {"b": 2}]\n',
    "nested-array": "[[1], [2]]\n",
    "nan": "NaN\n",
    "infinity": "Infinity\n",
    "neg-infinity": "-Infinity\n",
    "mixed-non-objects": 'null\ntrue\nfalse\n0\n""\n[]\n',
    "blank-lines-around-number": "\n\n42\n\n",
    "whitespace-padded-string": '  "x"  \n',
    "crlf-numbers": "0\r\n1\r\n",
    "stringified-object": '"{\\"a\\": 1}"\n',
}


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

    def _load(self, name: str, body: str, namespace: str = "ns"):
        legacy = self.cwd / name
        # Bytes so CRLF fixtures stay CRLF (text mode translates newlines).
        legacy.write_bytes(body.encode("utf-8"))
        database = str(self.cwd / (name + ".sqlite3"))
        events = load_events(database, namespace, legacy_path=str(legacy))
        return legacy, database, events


class Issue232NonObjectJsonlKeptTests(_CwdSandbox):
    def test_every_non_object_jsonl_keeps_bytes_and_inserts_nothing(self) -> None:
        for name, body in _NON_OBJECT_JSONL.items():
            with self.subTest(file=name):
                legacy, database, events = self._load(name + ".jsonl", body)
                self.assertEqual(events, [])
                self.assertTrue(legacy.is_file())
                self.assertEqual(legacy.read_bytes(), body.encode("utf-8"))
                self.assertEqual(self._event_count(database, "ns"), 0)

    def test_empty_and_whitespace_only_files_are_kept(self) -> None:
        cases = {
            "empty.jsonl": "",
            "spaces.jsonl": "   \n\t\n",
            "newlines.jsonl": "\n\n\n",
        }
        for name, body in cases.items():
            with self.subTest(file=name):
                legacy, database, events = self._load(name, body)
                self.assertEqual(events, [])
                self.assertTrue(legacy.is_file())
                self.assertEqual(legacy.read_bytes(), body.encode("utf-8"))
                self.assertEqual(self._event_count(database, "ns"), 0)

    def test_custom_pathlike_non_object_jsonl_is_kept(self) -> None:
        legacy = self.cwd / "like.jsonl"
        body = '42\n"x"\n'
        legacy.write_text(body, encoding="utf-8")
        value = _FsPath(str(legacy))
        self.assertFalse(hasattr(value, "exists"))
        self.assertFalse(hasattr(value, "unlink"))
        events = load_events(
            str(self.cwd / "like.sqlite3"), "ns", legacy_path=value
        )
        self.assertEqual(events, [])
        self.assertTrue(legacy.is_file())
        self.assertEqual(legacy.read_text(encoding="utf-8"), body)

    def test_pure_posix_path_non_object_jsonl_is_kept(self) -> None:
        legacy = self.cwd / "pure.jsonl"
        body = "null\n"
        legacy.write_text(body, encoding="utf-8")
        events = load_events(
            str(self.cwd / "pure.sqlite3"),
            "ns",
            legacy_path=PurePosixPath(legacy),
        )
        self.assertEqual(events, [])
        self.assertTrue(legacy.is_file())
        self.assertEqual(legacy.read_text(encoding="utf-8"), body)


class Issue232MixedAndErrorPathTests(_CwdSandbox):
    def test_mixed_object_and_non_object_imports_dicts_and_unlinks(self) -> None:
        # The issue unlinks once something was imported. Non-object lines are
        # skipped; the dicts become the system of record.
        legacy = self.cwd / "mixed.jsonl"
        legacy.write_text('42\n{"a": 1}\n"x"\n{"b": 2}\nnull\n', encoding="utf-8")
        database = str(self.cwd / "mixed.sqlite3")
        events = load_events(database, "ns", legacy_path=str(legacy))
        self.assertEqual(events, [{"b": 2}, {"a": 1}])
        self.assertFalse(legacy.exists())
        self.assertEqual(self._event_count(database, "ns"), 2)
        self.assertEqual(load_events(database, "ns"), [{"b": 2}, {"a": 1}])

    def test_parse_error_fail_closes_without_partial_import_or_unlink(self) -> None:
        # Pin the #207 truncated-file contract so a "unlink if any dict parsed
        # before the bad line" rewrite cannot land under #232.
        legacy = self.cwd / "truncated.jsonl"
        body = '{"a": 1}\nnot-json\n{"b": 2}\n'
        legacy.write_text(body, encoding="utf-8")
        database = str(self.cwd / "truncated.sqlite3")
        events = load_events(database, "ns", legacy_path=str(legacy))
        self.assertEqual(events, [])
        self.assertTrue(legacy.is_file())
        self.assertEqual(legacy.read_text(encoding="utf-8"), body)
        self.assertEqual(self._event_count(database, "ns"), 0)

    def test_populated_namespace_does_not_consume_a_non_object_jsonl(self) -> None:
        legacy = self.cwd / "stale.jsonl"
        body = '42\n"x"\n'
        legacy.write_text(body, encoding="utf-8")
        database = str(self.cwd / "stale.sqlite3")
        append_event(database, "ns", {"existing": True})
        events = load_events(database, "ns", legacy_path=str(legacy))
        self.assertEqual(events, [{"existing": True}])
        self.assertTrue(legacy.is_file())
        self.assertEqual(legacy.read_text(encoding="utf-8"), body)

    def test_other_namespace_does_not_inherit_a_failed_non_object_import(self) -> None:
        legacy = self.cwd / "shared.jsonl"
        body = "0\n"
        legacy.write_text(body, encoding="utf-8")
        database = str(self.cwd / "shared.sqlite3")
        first = load_events(database, "ns", legacy_path=str(legacy))
        second = load_events(database, "other", legacy_path=str(legacy))
        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertTrue(legacy.is_file())
        self.assertEqual(self._event_count(database, "ns"), 0)
        self.assertEqual(self._event_count(database, "other"), 0)

    def test_nested_object_jsonl_still_imports(self) -> None:
        legacy = self.cwd / "nested.jsonl"
        legacy.write_text('{"a": {"b": 1}}\n', encoding="utf-8")
        database = str(self.cwd / "nested.sqlite3")
        events = load_events(database, "ns", legacy_path=str(legacy))
        self.assertEqual(events, [{"a": {"b": 1}}])
        self.assertFalse(legacy.exists())

    def test_whitespace_padded_object_still_imports(self) -> None:
        legacy = self.cwd / "padded.jsonl"
        legacy.write_text('  {"a": 1}  \n', encoding="utf-8")
        database = str(self.cwd / "padded.sqlite3")
        events = load_events(database, "ns", legacy_path=str(legacy))
        self.assertEqual(events, [{"a": 1}])
        self.assertFalse(legacy.exists())


if __name__ == "__main__":
    unittest.main()
