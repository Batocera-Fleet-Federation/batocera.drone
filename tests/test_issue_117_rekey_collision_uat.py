"""User-level acceptance tests for issue #117: re-key collisions and placeholders.

Runs the movie sync the poller runs -- scrape, move or replace files, sync
again -- to cover the two shapes left behind after #114: several duplicate
scraped orphans (tmdb + tmdb_tv) claiming one live file, and a live file whose
only metadata row is an empty ``local`` placeholder written by artwork recovery.
Also covers the guarantee that a real row on the target is never replaced, and
that a second sync is a no-op.
"""

import os
import tempfile
import unittest
from pathlib import Path

import app.storage.movies_store as movies_store


class MovieRekeyCollisionUatTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.userdata = Path(self._tmp.name)
        self.movies_root = self.userdata / "movies"
        self.movies_root.mkdir(parents=True)
        self.shows_root = self.userdata / "shows"
        self.shows_root.mkdir(parents=True)
        self._db_env = os.environ.get("DRONE_STATE_DATABASE_FILE")
        os.environ["DRONE_STATE_DATABASE_FILE"] = str(self.userdata / "system" / "drone-app" / "cache.sqlite3")

    def tearDown(self):
        if self._db_env is None:
            os.environ.pop("DRONE_STATE_DATABASE_FILE", None)
        else:
            os.environ["DRONE_STATE_DATABASE_FILE"] = self._db_env
        self._tmp.cleanup()

    def _write(self, rel: str, data: bytes) -> Path:
        path = self.movies_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _entry_key(self, rel: str) -> str:
        return next(item["entry_key"] for item in movies_store.list_movies(self.movies_root) if item["file_path"] == rel)

    def _save(self, entry_key: str, provider: str, provider_id: str, title: str) -> None:
        movies_store.save_movie_metadata(
            self.movies_root, entry_key, provider=provider, provider_id=provider_id, title=title,
            poster_relative_path=None, backdrop_relative_path=None, extra={},
        )

    def test_duplicate_tmdb_and_tmdb_tv_orphans_move_one_winner_and_leave_no_extra_orphans(self):
        self._write("Show S01E01 (a).mkv", b"episode-bytes")
        self._write("Show S01E01 (b).mkv", b"episode-bytes")
        movies_store.sync_movies_cache(self.movies_root)
        self._save(self._entry_key("Show S01E01 (a).mkv"), "tmdb", "10", "Movie Row")
        self._save(self._entry_key("Show S01E01 (b).mkv"), "tmdb_tv", "10-s1e1", "Episode Row")

        (self.movies_root / "Show S01E01 (a).mkv").unlink()
        (self.movies_root / "Show S01E01 (b).mkv").unlink()
        self._write("Season 01/Show S01E01.mkv", b"episode-bytes")
        result = movies_store.sync_movies_cache(self.movies_root)

        new_key = self._entry_key("Season 01/Show S01E01.mkv")
        metadata = movies_store.get_movie_metadata(self.movies_root, new_key)
        self.assertEqual(metadata["provider"], "tmdb_tv")
        self.assertEqual(metadata["title"], "Episode Row")
        self.assertEqual(result["metadata_orphaned"], 2)
        self.assertEqual(result["metadata_rekeyed"], 1)
        self.assertEqual(result["metadata_superseded"], 1)
        self.assertEqual(result["metadata_unmatched"], 0)

        again = movies_store.sync_movies_cache(self.movies_root)
        self.assertEqual(again["metadata_orphaned"], 0)
        self.assertEqual(again["metadata_superseded"], 0)

    def test_local_placeholder_on_the_target_is_replaced_by_the_real_orphan(self):
        self._write("Vacation.mp4", b"aaaa-bytes")
        movies_store.sync_movies_cache(self.movies_root)
        self._save(self._entry_key("Vacation.mp4"), "tmdb", "7", "Vacation")

        # The original is moved to a new path holding different bytes, and
        # artwork recovery writes an empty local row on that new live entry.
        (self.movies_root / "Vacation.mp4").unlink()
        self._write("new/Vacation.mp4", b"bbbb-bytes")
        self._write("new/images/Vacation-tmdb-poster.jpg", b"poster")
        movies_store.sync_movies_cache(self.movies_root)
        target_key = self._entry_key("new/Vacation.mp4")
        placeholder = movies_store.get_movie_metadata(self.movies_root, target_key)
        self.assertEqual(placeholder["provider"], "local")
        self.assertEqual(placeholder["title"], "")

        # The new file now holds the original bytes, so the orphaned real row
        # claims the target and replaces the placeholder.
        self._write("new/Vacation.mp4", b"aaaa-bytes")
        result = movies_store.sync_movies_cache(self.movies_root)

        metadata = movies_store.get_movie_metadata(self.movies_root, target_key)
        self.assertEqual(metadata["provider"], "tmdb")
        self.assertEqual(metadata["title"], "Vacation")
        self.assertEqual(result["metadata_rekeyed"], 1)
        self.assertEqual(result["metadata_orphaned"], 1)

    def test_real_row_on_the_target_is_not_replaced_by_an_orphan(self):
        self._write("Vacation.mp4", b"aaaa-bytes")
        movies_store.sync_movies_cache(self.movies_root)
        self._save(self._entry_key("Vacation.mp4"), "tmdb", "7", "Vacation")

        (self.movies_root / "Vacation.mp4").unlink()
        self._write("new/Vacation.mp4", b"bbbb-bytes")
        movies_store.sync_movies_cache(self.movies_root)
        target_key = self._entry_key("new/Vacation.mp4")
        self._save(target_key, "tmdb", "99", "Live Row")

        # The original bytes return at the new path, but the target already
        # holds a real scraped row, so the orphan must stay put for review.
        self._write("new/Vacation.mp4", b"aaaa-bytes")
        result = movies_store.sync_movies_cache(self.movies_root)

        self.assertEqual(movies_store.get_movie_metadata(self.movies_root, target_key)["title"], "Live Row")
        self.assertEqual(result["metadata_orphaned"], 1)
        self.assertEqual(result["metadata_rekeyed"], 0)
        self.assertEqual(result["metadata_unmatched"], 1)


if __name__ == "__main__":
    unittest.main()
