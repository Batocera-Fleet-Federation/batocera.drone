"""Issue #232: source removal requires durable dictionary imports.

Derived from the reported data loss and the repository's transactional-import
rule: no accepted dictionary means preserve the source; an insertion or commit
failure must likewise preserve it for retry. A result limit controls returned
events, not whether migration succeeded. Fixtures use real SQLite transactions;
triggers inject deterministic failures without replacing SQL execution.
"""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from app.storage import state_store


class ImportDurabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "state.sqlite3"
        self.legacy = self.root / "events.jsonl"
        # Explicit close: sqlite3.Connection's context manager only ends a transaction.
        with closing(state_store.open_database(self.db)):
            pass

    def rows(self, namespace="ns"):
        with closing(sqlite3.connect(self.db)) as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM app_events WHERE namespace=? ORDER BY sequence",
                (namespace,),
            )]

    def load(self, **kwargs):
        return state_store.load_events(str(self.db), "ns", legacy_path=str(self.legacy), **kwargs)

    def test_zero_limit_does_not_confuse_empty_result_with_import_success(self):
        body = b'42\r\n"x"\r\n'
        self.legacy.write_bytes(body)
        for limit in (0, 1, -1):
            with self.subTest(limit=limit):
                self.assertEqual(self.load(limit=limit), [])
                self.assertEqual(self.legacy.read_bytes(), body)
                self.assertEqual(self.rows(), [])
        # Recovery by adding a real event must work even if the caller requests no rows.
        self.legacy.write_bytes(body + b'{}\n')
        self.assertEqual(self.load(limit=0), [])
        self.assertFalse(self.legacy.exists())
        self.assertEqual(self.rows(), [{}])
        self.assertEqual(state_store.load_events(self.db, "ns"), [{}])

    def test_second_insert_failure_rolls_back_and_keeps_source_for_retry(self):
        body = b'42\n{"first": 1}\n{"blocked": true}\n'
        self.legacy.write_bytes(body)
        state_store.append_event(self.db, "other", {"sentinel": True})
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON app_events
                WHEN NEW.namespace = 'ns' AND NEW.payload = '{"blocked": true}'
                BEGIN SELECT RAISE(ABORT, 'fixture rejection'); END""")
            connection.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fixture rejection"):
            self.load()
        self.assertEqual(self.legacy.read_bytes(), body)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows("other"), [{"sentinel": True}])
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("DROP TRIGGER reject_second")
            connection.commit()
        self.assertEqual(self.load(), [{"blocked": True}, {"first": 1}])
        self.assertFalse(self.legacy.exists())
        self.assertEqual(self.rows(), [{"first": 1}, {"blocked": True}])

    def test_commit_failure_preserves_source_and_rolls_back_inserted_rows(self):
        body = b'0\n{"event": 1}\n'
        self.legacy.write_bytes(body)
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executescript("""
                CREATE TABLE fixture_parent (id INTEGER PRIMARY KEY);
                CREATE TABLE fixture_child (parent_id INTEGER REFERENCES fixture_parent(id)
                    DEFERRABLE INITIALLY DEFERRED);
                CREATE TRIGGER reject_commit AFTER INSERT ON app_events
                    BEGIN INSERT INTO fixture_child VALUES (123); END;
            """)
        real_open = state_store.open_database
        connections = []

        def enforcing_open(path):
            connection = real_open(path)
            connection.execute("PRAGMA foreign_keys=ON")
            connections.append(connection)
            return connection

        # The INSERT succeeds; SQLite raises at COMMIT for the deferred constraint.
        try:
            with mock.patch.object(state_store, "open_database", side_effect=enforcing_open):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
                    self.load()
        finally:
            for connection in connections:
                connection.close()
        self.assertEqual(self.legacy.read_bytes(), body)
        self.assertEqual(self.rows(), [])
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM fixture_child").fetchone()[0], 0)
            connection.execute("DROP TRIGGER reject_commit")
            connection.commit()
        self.assertEqual(self.load(), [{"event": 1}])
        self.assertFalse(self.legacy.exists())
        self.assertEqual(self.rows(), [{"event": 1}])

    def test_unlink_error_occurs_after_commit_and_retry_does_not_duplicate(self):
        body = b'null\n{"event": 1}\n{}\n'
        self.legacy.write_bytes(body)

        def cannot_unlink(path, **kwargs):
            self.assertEqual(path, self.legacy)
            # A separate connection must already see all imported rows.
            self.assertEqual(self.rows(), [{"event": 1}, {}])
            raise PermissionError("fixture unlink denial")

        with mock.patch.object(Path, "unlink", autospec=True, side_effect=cannot_unlink) as unlink:
            self.assertEqual(self.load(), [{}, {"event": 1}])
            unlink.assert_called_once()
        self.assertEqual(self.legacy.read_bytes(), body)
        self.assertEqual(self.load(), [{}, {"event": 1}])
        self.assertEqual(self.rows(), [{"event": 1}, {}])

    def test_invalid_utf8_never_imports_a_valid_prefix_or_removes_source(self):
        body = b'{"prefix": 1}\n\xff\n42\n'
        self.legacy.write_bytes(body)
        self.assertEqual(self.load(), [])
        self.assertEqual(self.legacy.read_bytes(), body)
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
