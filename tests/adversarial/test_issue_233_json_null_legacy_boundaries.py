"""Adversarial malformed-input and boundary checks for issue #233.

Issue #233 is the miss-sentinel: a valid JSON ``null`` document must not be
confused with an unreadable or absent legacy file. Migration is a one-time
import of whatever ``json.loads`` returns from a readable file.

Expected, from the issue (null is a document) and the import invariant:

- Whitespace-padded and newline-terminated ``null`` still parse as JSON null
  and must import. ``json.loads`` accepts surrounding whitespace.
- The JSON string ``"null"`` is a string payload, not JSON null.
- Uppercase ``NULL``, empty files, whitespace-only files, truncated JSON,
  trailing garbage after ``null``, and directories are not readable JSON
  documents: return the fallback, keep the path, write no row.
- A populated state row wins over a still-present ``null`` file; the file is
  not unlinked and is not written over the existing row.
- A stored JSON-null row wins over a later rewritten object file: SQLite is
  already the system of record, so the leftover file must stay.
- ``state_key`` isolates the imported null; the default key stays a miss.
- Non-``Path`` ``os.PathLike`` and ``str`` paths follow the same null-import
  rule as pathlib (the #207 hole was type, this issue is the sentinel).
- After an invalid file is rewritten to ``null``, a retry must import it.
- Unlink failure after copying ``null`` must not lose the copied payload.
- JSON ``[null]`` is an array document, not a miss.

Cases run from an empty temporary cwd so stray files are visible.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from app.storage.state_store import load_payload, save_payload


class _FsPath:
    """os.PathLike that is not pathlib.Path and has no exists/read_text/unlink."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __fspath__(self) -> str:
        return self._value


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

    def _row(self, database: str, namespace: str, state_key: str = "payload"):
        with sqlite3.connect(database) as connection:
            return connection.execute(
                "SELECT payload FROM app_state WHERE namespace = ? AND state_key = ?",
                (namespace, state_key),
            ).fetchone()


class Issue233NullLexemeTests(_CwdSandbox):
    def test_whitespace_padded_null_imports(self) -> None:
        bodies = {
            "spaces": " null ",
            "newline": "null\n",
            "crlf": "null\r\n",
            "tabs": "\tnull\t",
            "blank-lines": "\n\nnull\n\n",
        }
        for label, text in bodies.items():
            with self.subTest(body=label):
                self.assertIsNone(json.loads(text))
                legacy = self.cwd / f"{label}.json"
                legacy.write_text(text, encoding="utf-8")
                database = str(self.cwd / f"{label}.sqlite3")
                result = load_payload(
                    database, "ns", {"fallback": True}, legacy_path=str(legacy)
                )
                self.assertIsNone(result)
                self.assertFalse(legacy.exists())
                self.assertEqual(self._row(database, "ns")[0].strip(), "null")

    def test_json_string_null_is_the_string_not_none(self) -> None:
        legacy = self.cwd / "quoted.json"
        legacy.write_text('"null"', encoding="utf-8")
        database = str(self.cwd / "quoted.sqlite3")
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(result, "null")
        self.assertIsInstance(result, str)
        self.assertFalse(legacy.exists())
        self.assertEqual(load_payload(database, "ns", {"fallback": True}), "null")

    def test_array_containing_null_is_a_document(self) -> None:
        legacy = self.cwd / "array-null.json"
        legacy.write_text("[null]", encoding="utf-8")
        database = str(self.cwd / "array-null.sqlite3")
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(result, [None])
        self.assertFalse(legacy.exists())
        self.assertEqual(load_payload(database, "ns", {"fallback": True}), [None])


class Issue233UnreadableKeepTests(_CwdSandbox):
    def test_non_json_null_lexemes_are_misses(self) -> None:
        bodies = {
            "uppercase": "NULL",
            "title": "Null",
            "undefined": "undefined",
            "empty": "",
            "whitespace": "   \n",
            "trailing-garbage": "null true",
            "double": "null\nnull\n",
            "python-none": "None",
            "truncated": "nul",
        }
        for label, text in bodies.items():
            with self.subTest(body=label):
                with self.assertRaises(json.JSONDecodeError):
                    json.loads(text)
                legacy = self.cwd / f"{label}.json"
                legacy.write_text(text, encoding="utf-8")
                database = str(self.cwd / f"{label}.sqlite3")
                result = load_payload(
                    database, "ns", {"fallback": True}, legacy_path=str(legacy)
                )
                self.assertEqual(result, {"fallback": True})
                self.assertTrue(legacy.is_file())
                self.assertEqual(legacy.read_text(encoding="utf-8"), text)
                self.assertIsNone(self._row(database, "ns"))

    def test_utf8_bom_prefixed_null_is_unreadable_and_kept(self) -> None:
        # json.loads rejects a leading BOM; this is not the JSON document null.
        legacy = self.cwd / "bom-null.json"
        legacy.write_bytes(b"\xef\xbb\xbfnull")
        database = str(self.cwd / "bom-null.sqlite3")
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(result, {"fallback": True})
        self.assertTrue(legacy.is_file())
        self.assertEqual(legacy.read_bytes(), b"\xef\xbb\xbfnull")
        self.assertIsNone(self._row(database, "ns"))

    def test_directory_legacy_path_is_a_miss(self) -> None:
        directory = self.cwd / "legacy-dir"
        directory.mkdir()
        (directory / "nested.txt").write_text("keep", encoding="utf-8")
        database = str(self.cwd / "dir.sqlite3")
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(directory)
        )
        self.assertEqual(result, {"fallback": True})
        self.assertTrue(directory.is_dir())
        self.assertEqual((directory / "nested.txt").read_text(encoding="utf-8"), "keep")
        self.assertIsNone(self._row(database, "ns"))

    def test_invalid_rewritten_to_null_imports_on_retry(self) -> None:
        legacy = self.cwd / "retry.json"
        legacy.write_text("{not json", encoding="utf-8")
        database = str(self.cwd / "retry.sqlite3")
        first = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(first, {"fallback": True})
        self.assertTrue(legacy.is_file())
        legacy.write_text("null", encoding="utf-8")
        second = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertIsNone(second)
        self.assertFalse(legacy.exists())
        self.assertIsNone(load_payload(database, "ns", {"fallback": True}))


class Issue233SqlitePrecedenceTests(_CwdSandbox):
    def test_populated_object_row_wins_over_null_legacy_file(self) -> None:
        legacy = self.cwd / "stale-null.json"
        legacy.write_text("null", encoding="utf-8")
        database = str(self.cwd / "stale-null.sqlite3")
        save_payload(database, "ns", {"existing": True})
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(result, {"existing": True})
        self.assertTrue(legacy.is_file())
        self.assertEqual(legacy.read_text(encoding="utf-8"), "null")

    def test_stored_null_row_wins_over_later_object_file(self) -> None:
        database = str(self.cwd / "stored-null.sqlite3")
        save_payload(database, "ns", None)
        leftover = self.cwd / "later.json"
        leftover.write_text('{"migrated": true}', encoding="utf-8")
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(leftover)
        )
        self.assertIsNone(result)
        self.assertTrue(leftover.is_file())
        self.assertEqual(leftover.read_text(encoding="utf-8"), '{"migrated": true}')

    def test_other_namespace_does_not_inherit_imported_null(self) -> None:
        legacy = self.cwd / "shared-null.json"
        legacy.write_text("null", encoding="utf-8")
        database = str(self.cwd / "shared-null.sqlite3")
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertIsNone(result)
        self.assertEqual(
            load_payload(database, "other", {"fallback": True}), {"fallback": True}
        )

    def test_custom_state_key_isolates_imported_null(self) -> None:
        legacy = self.cwd / "keyed-null.json"
        legacy.write_text("null", encoding="utf-8")
        database = str(self.cwd / "keyed-null.sqlite3")
        result = load_payload(
            database,
            "ns",
            {"fallback": True},
            legacy_path=str(legacy),
            state_key="alt",
        )
        self.assertIsNone(result)
        self.assertFalse(legacy.exists())
        self.assertIsNone(
            load_payload(database, "ns", {"fallback": True}, state_key="alt")
        )
        self.assertEqual(
            load_payload(database, "ns", {"fallback": True}), {"fallback": True}
        )
        self.assertIsNone(self._row(database, "ns", "payload"))
        self.assertEqual(self._row(database, "ns", "alt")[0].strip(), "null")


class Issue233PathTypeTests(_CwdSandbox):
    def test_custom_pathlike_json_null_migrates(self) -> None:
        legacy = self.cwd / "like-null.json"
        legacy.write_text("null", encoding="utf-8")
        value = _FsPath(str(legacy))
        self.assertFalse(hasattr(value, "exists"))
        result = load_payload(
            self.cwd / "like-null.sqlite3",
            "ns",
            {"fallback": True},
            legacy_path=value,
        )
        self.assertIsNone(result)
        self.assertFalse(legacy.exists())

    def test_pure_posix_path_json_null_migrates(self) -> None:
        legacy = self.cwd / "pure-null.json"
        legacy.write_text("null", encoding="utf-8")
        value = PurePosixPath(legacy)
        self.assertFalse(hasattr(value, "exists"))
        result = load_payload(
            self.cwd / "pure-null.sqlite3",
            "ns",
            {"fallback": True},
            legacy_path=value,
        )
        self.assertIsNone(result)
        self.assertFalse(legacy.exists())

    def test_relative_str_null_imports_from_cwd(self) -> None:
        (self.cwd / "rel-null.json").write_text("null", encoding="utf-8")
        result = load_payload(
            "rel-null.sqlite3", "ns", {"fallback": True}, legacy_path="rel-null.json"
        )
        self.assertIsNone(result)
        self.assertFalse((self.cwd / "rel-null.json").exists())
        self.assertTrue((self.cwd / "rel-null.sqlite3").is_file())


class Issue233UnlinkFailureTests(_CwdSandbox):
    def test_unlink_oserror_still_returns_stored_null_from_db(self) -> None:
        db_dir = self.cwd / "db"
        legacy_dir = self.cwd / "legacy-parent"
        db_dir.mkdir()
        legacy_dir.mkdir()
        legacy = legacy_dir / "null.json"
        legacy.write_text("null", encoding="utf-8")
        database = str(db_dir / "state.sqlite3")
        os.chmod(legacy_dir, 0o555)
        try:
            result = load_payload(
                database, "ns", {"fallback": True}, legacy_path=str(legacy)
            )
            self.assertIsNone(result)
            self.assertIsNone(
                load_payload(
                    database, "ns", {"fallback": True}, legacy_path=str(legacy)
                )
            )
            self.assertEqual(self._row(database, "ns")[0].strip(), "null")
        finally:
            os.chmod(legacy_dir, 0o755)


class Issue233NonPathSkipTests(_CwdSandbox):
    def test_none_legacy_path_is_a_miss_not_a_null_document(self) -> None:
        database = str(self.cwd / "no-legacy.sqlite3")
        result = load_payload(database, "ns", {"fallback": True}, legacy_path=None)
        self.assertEqual(result, {"fallback": True})
        self.assertIsNone(self._row(database, "ns"))


if __name__ == "__main__":
    unittest.main()
