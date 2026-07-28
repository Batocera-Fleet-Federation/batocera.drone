import os
import tempfile
import unittest
from pathlib import Path

import app.storage.movies_store as movies_store


class MoviesStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.userdata = Path(self._tmp.name)
        self.movies_root = self.userdata / "movies"
        self.movies_root.mkdir(parents=True)
        # Keep the SQLite cache inside the temp dir.
        self._db_env = os.environ.get("DRONE_STATE_DATABASE_FILE")
        os.environ["DRONE_STATE_DATABASE_FILE"] = str(self.userdata / "system" / "drone-app" / "cache.sqlite3")

    def tearDown(self):
        if self._db_env is None:
            os.environ.pop("DRONE_STATE_DATABASE_FILE", None)
        else:
            os.environ["DRONE_STATE_DATABASE_FILE"] = self._db_env
        self._tmp.cleanup()

    def _write(self, rel, data=b"movie-data"):
        path = self.movies_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_scan_has_no_system_dimension(self):
        self._write("Vacation Highlights.mp4")
        entries = movies_store.scan_movies(self.movies_root)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.file_path, "Vacation Highlights.mp4")
        self.assertEqual(entry.movie_name, "Vacation Highlights.mp4")
        self.assertFalse(hasattr(entry, "system"))
        self.assertTrue(entry.fingerprint)

    def test_scan_ignores_partial_and_lock_files(self):
        self._write("Real Movie.mp4")
        self._write("Downloading.mp4.part")
        self._write("Real Movie.mp4.lock")
        entries = movies_store.scan_movies(self.movies_root)
        self.assertEqual([entry.file_path for entry in entries], ["Real Movie.mp4"])

    def test_fingerprint_matches_rom_repository_algorithm(self):
        path = self._write("Game Trailer.mp4", b"x" * 1000)
        # Same sampled-hash algorithm as RomRepository.build_fingerprint / saves.
        from app.drone_api import RomRepository

        self.assertEqual(movies_store.build_movie_fingerprint(path), RomRepository.build_fingerprint(path))

    def test_sync_reports_created_updated_deleted(self):
        self._write("clips/A.mp4", b"one")
        self._write("clips/B.mp4", b"two")
        first = movies_store.sync_movies_cache(self.movies_root)
        self.assertEqual(first["created"], 2)
        self.assertEqual(first["updated"], 0)
        self.assertEqual(first["total"], 2)

        # No-op rescan is clean.
        second = movies_store.sync_movies_cache(self.movies_root)
        self.assertEqual((second["created"], second["updated"], second["deleted"]), (0, 0, 0))

        # Update one, delete another.
        self._write("clips/A.mp4", b"one-changed-and-longer")
        (self.movies_root / "clips" / "B.mp4").unlink()
        third = movies_store.sync_movies_cache(self.movies_root)
        self.assertEqual(third["updated"], 1)
        self.assertEqual(third["deleted"], 1)

    def test_pending_changes_queue_and_clear(self):
        self._write("clips/A.mp4")
        movies_store.sync_movies_cache(self.movies_root)
        pending = movies_store.read_pending_changes(self.movies_root)
        self.assertEqual(len(pending["movies"]), 1)
        self.assertEqual(pending["movies"][0]["file_path"], "clips/A.mp4")

        movies_store.clear_pending_changes(self.movies_root)
        self.assertEqual(movies_store.read_pending_changes(self.movies_root), {"movies": [], "deleted": []})

        (self.movies_root / "clips" / "A.mp4").unlink()
        movies_store.sync_movies_cache(self.movies_root)
        deleted = movies_store.read_pending_changes(self.movies_root)
        self.assertEqual(len(deleted["deleted"]), 1)
        self.assertEqual(deleted["deleted"][0]["file_path"], "clips/A.mp4")

    def test_thumbprint_changes_with_content_and_is_stable(self):
        self._write("clips/A.mp4", b"one")
        movies_store.sync_movies_cache(self.movies_root)
        tp1 = movies_store.stored_thumbprint(self.movies_root)
        # Stable across rescans of identical content.
        movies_store.sync_movies_cache(self.movies_root)
        self.assertEqual(tp1, movies_store.stored_thumbprint(self.movies_root))
        # Changes when a movie changes.
        self._write("clips/A.mp4", b"one-changed-and-longer")
        movies_store.sync_movies_cache(self.movies_root)
        self.assertNotEqual(tp1, movies_store.stored_thumbprint(self.movies_root))

    def test_list_movies_returns_upload_ready_payload(self):
        self._write("clips/A.mp4")
        self._write("clips/B.mp4")
        movies_store.sync_movies_cache(self.movies_root)
        items = movies_store.list_movies(self.movies_root)
        self.assertEqual(len(items), 2)
        self.assertNotIn("system", items[0])
        self.assertIn("movies_fingerprint", items[0])

    def test_list_movies_page_filters_by_query_and_paginates(self):
        self._write("clips/Alpha.mp4")
        self._write("clips/Beta.mp4")
        self._write("clips/Gamma.mp4")
        movies_store.sync_movies_cache(self.movies_root)

        page = movies_store.list_movies_page(self.movies_root, limit=2, offset=0)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["items"]), 2)

        filtered = movies_store.list_movies_page(self.movies_root, query="beta")
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["items"][0]["movie_name"], "Beta.mp4")


if __name__ == "__main__":
    unittest.main()
