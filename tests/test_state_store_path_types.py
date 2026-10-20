"""State DB path helpers accept every path type the guard allows (str, PathLike)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage import state_store


class StateStorePathTypeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_open_database_accepts_str_path(self):
        connection = state_store.open_database(str(self.root / "x.sqlite3"))
        self.addCleanup(connection.close)
        self.assertTrue((self.root / "x.sqlite3").is_file())

    def test_open_database_accepts_pathlike_path(self):
        connection = state_store.open_database(self.root / "y.sqlite3")
        self.addCleanup(connection.close)
        self.assertTrue((self.root / "y.sqlite3").is_file())

    def test_database_path_for_legacy_file_accepts_str(self):
        resolved = state_store.database_path_for_legacy_file(str(self.root / "cred.json"))
        self.assertIsInstance(resolved, Path)
        self.assertEqual(resolved.parent, self.root.resolve())

    def test_database_path_for_legacy_file_accepts_pathlike(self):
        resolved = state_store.database_path_for_legacy_file(self.root / "cred.json")
        self.assertEqual(resolved.parent, self.root.resolve())

    def test_non_path_values_are_still_rejected(self):
        with self.assertRaises(TypeError):
            state_store.open_database(12345)
        with self.assertRaises(TypeError):
            state_store.database_path_for_legacy_file(None)

    def test_empty_and_whitespace_paths_are_rejected(self):
        for bad in ("", "   ", "\t\n", Path("")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    state_store.open_database(bad)
        with self.assertRaises(ValueError):
            state_store.database_path_for_legacy_file("")
        with self.assertRaises(ValueError):
            state_store.database_path("  ")

    def test_legacy_file_helper_rejects_directory_shaped_paths(self):
        # Path('') normalizes to '.', so these all name the working directory, not a legacy file.
        for bad in ("", ".", "./", Path(""), Path("."), "..", "/"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    state_store.database_path_for_legacy_file(bad)

    def test_legacy_file_helper_does_not_derive_database_beside_cwd_parent(self):
        cwd = self.root / "work"
        cwd.mkdir()
        previous = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, previous)
        with self.assertRaises(ValueError):
            state_store.database_path_for_legacy_file(Path(""))
        self.assertFalse((self.root / state_store.DATABASE_FILENAME).exists())

    def test_credential_store_rejects_blank_credentials_path_even_with_injected_database(self):
        from app.common.auth import DroneCredentialStore

        cwd = self.root / "work"
        cwd.mkdir()
        previous = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, previous)
        database = self.root / "state.sqlite3"
        for bad in ("", ".", Path(""), Path("."), "..", "/"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    DroneCredentialStore(bad, state_database_file=database)
        self.assertFalse(database.exists())

    def test_legacy_file_reader_ignores_directory_shaped_paths(self):
        for bad in ("", ".", "..", "/", Path("")):
            with self.subTest(bad=bad):
                self.assertIsNone(state_store._legacy_file_path(bad))

    def test_format_only_paths_are_rejected_before_sqlite(self):
        # U+200B and U+FEFF are category Cf: str.strip() keeps them, so they must be blank here.
        for bad in ("​", "﻿", " ​⁠ ", Path("​")):
            with self.subTest(bad=bad):
                with mock.patch.object(state_store.sqlite3, "connect") as connect:
                    with self.assertRaises(ValueError):
                        state_store.open_database(bad)
                    connect.assert_not_called()
                with self.assertRaises(ValueError):
                    state_store.database_path_for_legacy_file(bad)

    def test_invisible_final_component_is_rejected_as_legacy_file(self):
        # Hangul filler, Braille blank, zero-width space and friends render as nothing, so a
        # legacy path ending in one must not derive a database beside an unnameable file.
        invisible = ("\u3164", "\u2800", "\u200b", "\u115f", "\u1160", "\uffa0", "\ufeff", "\u3000", " ")
        for char in invisible:
            for bad in (char, f"dir/{char}", f"dir/{char}/", Path("dir") / char):
                with self.subTest(char=char, bad=bad):
                    with self.assertRaises(ValueError):
                        state_store.require_legacy_file_path(bad)
                    with self.assertRaises(ValueError):
                        state_store.database_path_for_legacy_file(bad)
                    self.assertIsNone(state_store._legacy_file_path(bad))

    def test_credential_store_rejects_invisible_named_credentials_file(self):
        from app.common.auth import DroneCredentialStore

        for char in ("\u3164", "\u2800", "\u200b"):
            for bad in (char, str(self.root / "dir" / char)):
                with self.subTest(char=char, bad=bad):
                    with self.assertRaises(ValueError):
                        DroneCredentialStore(bad)
                    with self.assertRaises(ValueError):
                        DroneCredentialStore(bad, state_database_file=self.root / "state.sqlite3")
        self.assertFalse((self.root / "dir").exists())
        self.assertFalse((self.root / "state.sqlite3").exists())

    def test_credentials_name_containing_invisible_character_is_still_valid(self):
        from app.common.auth import DroneCredentialStore

        store = DroneCredentialStore(self.root / "cred\u200bs.json")
        self.assertEqual(store.state_database_file.name, state_store.DATABASE_FILENAME)

    def test_name_containing_format_character_is_still_a_valid_path(self):
        target = self.root / "a​b.sqlite3"
        connection = state_store.open_database(target)
        self.addCleanup(connection.close)
        self.assertTrue(target.is_file())

    def test_empty_database_path_creates_no_files(self):
        before = set(os.listdir(os.getcwd()))
        with self.assertRaises(ValueError):
            state_store.open_database("")
        self.assertEqual(set(os.listdir(os.getcwd())), before)

    def _legacy_json(self, name="legacy.json"):
        legacy = self.root / name
        legacy.write_text('{"migrated": true}', encoding="utf-8")
        return legacy

    def test_load_payload_migrates_str_legacy_path(self):
        legacy = self._legacy_json()
        result = state_store.load_payload(
            str(self.root / "s.sqlite3"), "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(result, {"migrated": True})
        self.assertFalse(legacy.exists())

    def test_load_payload_migrates_pathlike_legacy_path(self):
        legacy = self._legacy_json()
        result = state_store.load_payload(
            self.root / "p.sqlite3", "ns", {"fallback": True}, legacy_path=legacy
        )
        self.assertEqual(result, {"migrated": True})
        self.assertFalse(legacy.exists())

    def test_load_payload_ignores_non_path_legacy_path(self):
        result = state_store.load_payload(
            self.root / "m.sqlite3", "ns", {"fallback": True}, legacy_path=mock.MagicMock()
        )
        self.assertEqual(result, {"fallback": True})

    def test_load_payload_migrates_json_null_legacy_file(self):
        legacy = self.root / "null.json"
        legacy.write_text("null", encoding="utf-8")
        db = self.root / "n.sqlite3"
        result = state_store.load_payload(str(db), "ns", {"fallback": True}, legacy_path=str(legacy))
        self.assertIsNone(result)
        self.assertFalse(legacy.exists())
        # The null document was persisted, so it wins over the fallback on the next load.
        self.assertIsNone(state_store.load_payload(str(db), "ns", {"fallback": True}))

    def test_load_payload_keeps_unreadable_or_absent_legacy_file(self):
        bad = self.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        for legacy in (bad, self.root / "absent.json"):
            with self.subTest(legacy=legacy.name):
                result = state_store.load_payload(
                    self.root / "u.sqlite3", "ns", {"fallback": True}, legacy_path=legacy
                )
                self.assertEqual(result, {"fallback": True})
        self.assertTrue(bad.exists())

    def test_load_events_migrates_str_legacy_path(self):
        legacy = self.root / "events.jsonl"
        legacy.write_text('{"a": 1}\n', encoding="utf-8")
        events = state_store.load_events(
            str(self.root / "e.sqlite3"), "ns", legacy_path=str(legacy)
        )
        self.assertEqual(events, [{"a": 1}])
        self.assertFalse(legacy.exists())

    def test_load_events_keeps_legacy_file_with_no_dict_entries(self):
        legacy = self.root / "events.jsonl"
        legacy.write_text('42\n"x"\n', encoding="utf-8")
        events = state_store.load_events(
            str(self.root / "e.sqlite3"), "ns", legacy_path=str(legacy)
        )
        self.assertEqual(events, [])
        self.assertTrue(legacy.exists())


if __name__ == "__main__":
    unittest.main()
