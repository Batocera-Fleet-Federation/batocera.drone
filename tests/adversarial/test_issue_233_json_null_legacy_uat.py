"""Adversarial UAT for issue #233: JSON null is a real legacy document.

Issue #233: ``load_payload`` treated a legacy file whose contents are the JSON
document ``null`` as a missing file. ``json.loads('null')`` is Python ``None``,
and the one-time import only ran when the parsed value was not ``None``. The
reported repro writes ``null`` to a temp file and calls
``load_payload(str(db), 'ns', {'fallback': True}, legacy_path=str(file))``.
The result was the fallback and the file stayed on disk.

Expected behavior, derived from that report and the one-time JSON import
invariant (SQLite is the system of record only after a real document is
copied), before treating the delivery tests as ground truth:

- A readable file whose JSON value is ``null`` is a document. ``load_payload``
  must return ``None`` (not the fallback), persist that document in SQLite, and
  unlink the file.
- A later load with no legacy path still returns ``None`` even when a non-None
  fallback is supplied: the stored null wins because a row exists.
- The stored ``app_state.payload`` text is JSON ``null``, which is how a
  missing namespace (no row) is distinguished from an imported null document
  when the caller also passed ``fallback=None``.
- Other falsy JSON (``false``, ``0``, ``""``, ``[]``, ``{}``) remains a
  document; the miss is "no readable file", not "Python-falsy payload".
- Production callers that forward a filesystem ``legacy_path`` (credentials,
  game-log cursors) must not crash when that file is JSON ``null``.

Cases run from an empty temporary cwd with a cleared environment.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.auth import DroneCredentialStore
from app.storage.state_store import load_payload, save_payload


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


class Issue233ReportedReproductionTests(_CwdSandbox):
    def test_json_null_legacy_file_returns_none_and_unlinks(self) -> None:
        # Shape of the reported repro: write null, then load_payload(str(db),
        # 'ns', {'fallback': True}, legacy_path=str(file)).
        legacy = self.cwd / "null.json"
        legacy.write_text("null", encoding="utf-8")
        database = str(self.cwd / "n.sqlite3")
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertIsNone(result)
        self.assertFalse(legacy.exists())
        self.assertTrue(Path(database).is_file())
        row = self._row(database, "ns")
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row[0]), None)
        self.assertEqual(row[0].strip(), "null")

    def test_migrated_null_survives_a_second_load_without_the_file(self) -> None:
        legacy = self.cwd / "keep-null.json"
        legacy.write_text("null", encoding="utf-8")
        database = str(self.cwd / "persist.sqlite3")
        first = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertIsNone(first)
        self.assertFalse(legacy.exists())
        second = load_payload(database, "ns", {"fallback": True})
        self.assertIsNone(second)
        third = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertIsNone(third)

    def test_path_legacy_path_json_null_still_migrates(self) -> None:
        legacy = self.cwd / "path-null.json"
        legacy.write_text("null", encoding="utf-8")
        result = load_payload(
            self.cwd / "path-null.sqlite3",
            "ns",
            {"fallback": True},
            legacy_path=legacy,
        )
        self.assertIsNone(result)
        self.assertFalse(legacy.exists())


class Issue233MissingIsNotNullTests(_CwdSandbox):
    def test_absent_file_returns_fallback_and_writes_no_row(self) -> None:
        missing = self.cwd / "absent.json"
        database = str(self.cwd / "absent.sqlite3")
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(missing)
        )
        self.assertEqual(result, {"fallback": True})
        self.assertFalse(missing.exists())
        self.assertIsNone(self._row(database, "ns"))

    def test_unreadable_json_returns_fallback_keeps_file_and_writes_no_row(self) -> None:
        legacy = self.cwd / "bad.json"
        legacy.write_text("{not json", encoding="utf-8")
        database = str(self.cwd / "bad.sqlite3")
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(result, {"fallback": True})
        self.assertTrue(legacy.is_file())
        self.assertEqual(legacy.read_text(encoding="utf-8"), "{not json")
        self.assertIsNone(self._row(database, "ns"))

    def test_fallback_none_still_persists_json_null_as_a_row(self) -> None:
        # Return value is None for both a miss and a null document when the
        # caller passed fallback=None. The row (and unlink) are the contract.
        null_file = self.cwd / "doc-null.json"
        null_file.write_text("null", encoding="utf-8")
        null_db = str(self.cwd / "doc-null.sqlite3")
        self.assertIsNone(load_payload(null_db, "ns", None, legacy_path=str(null_file)))
        self.assertFalse(null_file.exists())
        self.assertEqual(self._row(null_db, "ns")[0].strip(), "null")

        miss_db = str(self.cwd / "doc-miss.sqlite3")
        self.assertIsNone(
            load_payload(miss_db, "ns", None, legacy_path=str(self.cwd / "nope.json"))
        )
        self.assertIsNone(self._row(miss_db, "ns"))


class Issue233FalsyJsonStillDocumentsTests(_CwdSandbox):
    def test_other_falsy_json_values_still_migrate(self) -> None:
        cases = {
            "false.json": ("false", False),
            "zero.json": ("0", 0),
            "empty-str.json": ('""', ""),
            "empty-list.json": ("[]", []),
            "empty-obj.json": ("{}", {}),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(file=name):
                legacy = self.cwd / name
                legacy.write_text(text, encoding="utf-8")
                database = str(self.cwd / (name + ".sqlite3"))
                result = load_payload(
                    database, "ns", {"fallback": True}, legacy_path=str(legacy)
                )
                self.assertEqual(result, expected)
                self.assertFalse(legacy.exists())
                self.assertEqual(
                    load_payload(database, "ns", {"fallback": True}), expected
                )


class Issue233PersistenceWithoutLegacyTests(_CwdSandbox):
    def test_save_payload_none_is_a_stored_document_not_a_miss(self) -> None:
        database = str(self.cwd / "saved-null.sqlite3")
        save_payload(database, "ns", None)
        self.assertIsNone(load_payload(database, "ns", {"fallback": True}))
        self.assertEqual(self._row(database, "ns")[0].strip(), "null")


class Issue233ProductionCallerTests(_CwdSandbox):
    def test_credential_store_load_does_not_crash_on_json_null_file(self) -> None:
        credentials = self.cwd / "system" / "drone-app" / "credentials.json"
        credentials.parent.mkdir(parents=True, exist_ok=True)
        credentials.write_text("null", encoding="utf-8")
        database = str(credentials.parent / "rom_metadata_cache.sqlite3")
        loaded = DroneCredentialStore(
            str(credentials), state_database_file=database
        ).load()
        self.assertIsInstance(loaded, dict)
        self.assertIn("username", loaded)
        # Null is a document: the one-time import consumes the file. The
        # store then rejects a non-dict payload and uses defaults.
        self.assertFalse(credentials.exists())
        self.assertEqual(self._row(database, "credentials")[0].strip(), "null")
        again = DroneCredentialStore(
            str(credentials), state_database_file=database
        ).load()
        self.assertIsInstance(again, dict)
        self.assertEqual(again.get("username"), loaded.get("username"))

    def test_object_with_null_field_is_not_treated_as_missing(self) -> None:
        # Production cursors/credentials are objects; a null *field* must not
        # trip the old ``is not None`` miss check (the value is a dict).
        legacy = self.cwd / "cursors.json"
        legacy.write_text('{"epoch": null}', encoding="utf-8")
        database = str(self.cwd / "cursors.sqlite3")
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(result, {"epoch": None})
        self.assertFalse(legacy.exists())
        self.assertEqual(
            load_payload(database, "ns", {"fallback": True}), {"epoch": None}
        )


if __name__ == "__main__":
    unittest.main()
