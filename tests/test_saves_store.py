import os
import tempfile
import unittest
from pathlib import Path

import app.storage.saves_store as saves_store


class SavesStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.userdata = Path(self._tmp.name)
        self.saves_root = self.userdata / "saves"
        (self.saves_root / "snes").mkdir(parents=True)
        (self.saves_root / "gba").mkdir(parents=True)
        # Keep the SQLite cache inside the temp dir.
        self._db_env = os.environ.get("DRONE_STATE_DATABASE_FILE")
        os.environ["DRONE_STATE_DATABASE_FILE"] = str(self.userdata / "system" / "drone-app" / "cache.sqlite3")

    def tearDown(self):
        if self._db_env is None:
            os.environ.pop("DRONE_STATE_DATABASE_FILE", None)
        else:
            os.environ["DRONE_STATE_DATABASE_FILE"] = self._db_env
        self._tmp.cleanup()

    def _write(self, rel, data=b"save-data"):
        path = self.saves_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_scan_detects_system_and_fingerprint(self):
        self._write("snes/Chrono Trigger.srm")
        entries = saves_store.scan_saves(self.saves_root)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.system, "snes")
        self.assertEqual(entry.file_path, "snes/Chrono Trigger.srm")
        self.assertTrue(entry.fingerprint)

    def test_fingerprint_matches_rom_repository_algorithm(self):
        path = self._write("snes/Game.srm", b"x" * 1000)
        # Same sampled-hash algorithm as RomRepository.build_fingerprint.
        from app.drone_api import RomRepository

        self.assertEqual(saves_store.build_save_fingerprint(path), RomRepository.build_fingerprint(path))

    def test_sync_reports_created_updated_deleted(self):
        self._write("snes/A.srm", b"one")
        self._write("gba/B.srm", b"two")
        first = saves_store.sync_saves_cache(self.saves_root)
        self.assertEqual(first["created"], 2)
        self.assertEqual(first["updated"], 0)
        self.assertEqual(first["total"], 2)

        # No-op rescan is clean.
        second = saves_store.sync_saves_cache(self.saves_root)
        self.assertEqual((second["created"], second["updated"], second["deleted"]), (0, 0, 0))

        # Update one, delete another.
        self._write("snes/A.srm", b"one-changed-and-longer")
        (self.saves_root / "gba" / "B.srm").unlink()
        third = saves_store.sync_saves_cache(self.saves_root)
        self.assertEqual(third["updated"], 1)
        self.assertEqual(third["deleted"], 1)

    def test_pending_changes_queue_and_clear(self):
        self._write("snes/A.srm")
        saves_store.sync_saves_cache(self.saves_root)
        pending = saves_store.read_pending_changes(self.saves_root)
        self.assertEqual(len(pending["saves"]), 1)
        self.assertEqual(pending["saves"][0]["system"], "snes")

        saves_store.clear_pending_changes(self.saves_root)
        self.assertEqual(saves_store.read_pending_changes(self.saves_root), {"saves": [], "deleted": []})

        (self.saves_root / "snes" / "A.srm").unlink()
        saves_store.sync_saves_cache(self.saves_root)
        deleted = saves_store.read_pending_changes(self.saves_root)
        self.assertEqual(len(deleted["deleted"]), 1)
        self.assertEqual(deleted["deleted"][0]["file_path"], "snes/A.srm")

    def test_thumbprint_changes_with_content_and_is_stable(self):
        self._write("snes/A.srm", b"one")
        saves_store.sync_saves_cache(self.saves_root)
        tp1 = saves_store.stored_thumbprint(self.saves_root)
        # Stable across rescans of identical content.
        saves_store.sync_saves_cache(self.saves_root)
        self.assertEqual(tp1, saves_store.stored_thumbprint(self.saves_root))
        # Changes when a save changes.
        self._write("snes/A.srm", b"one-changed-and-longer")
        saves_store.sync_saves_cache(self.saves_root)
        self.assertNotEqual(tp1, saves_store.stored_thumbprint(self.saves_root))

    def test_list_saves_filterable_by_system(self):
        self._write("snes/A.srm")
        self._write("gba/B.srm")
        saves_store.sync_saves_cache(self.saves_root)
        self.assertEqual(len(saves_store.list_saves(self.saves_root)), 2)
        snes_only = saves_store.list_saves(self.saves_root, system="snes")
        self.assertEqual(len(snes_only), 1)
        self.assertEqual(snes_only[0]["system"], "snes")


if __name__ == "__main__":
    unittest.main()
