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

    def test_scan_traverses_all_sibling_and_nested_subfolders(self):
        # Real-world layout: multiple sibling category folders, one of them
        # nested a level deeper still (per-show subfolders under "shows").
        # os.walk() recurses into every directory it finds unless the walker
        # explicitly prunes its dirnames list -- _iter_movie_files never does,
        # so this must find every file regardless of how deep it's nested.
        self._write("Vacation.mp4")
        self._write("horror/The Shining.mp4")
        self._write("comedy/Airplane.mp4")
        self._write("shows/Breaking Bad/S01E01.mp4")
        self._write("shows/Breaking Bad/S01E02.mp4")
        self._write("shows/The Office/S02E05.mp4")
        entries = movies_store.scan_movies(self.movies_root)
        self.assertEqual(
            sorted(entry.file_path for entry in entries),
            [
                "Vacation.mp4",
                "comedy/Airplane.mp4",
                "horror/The Shining.mp4",
                "shows/Breaking Bad/S01E01.mp4",
                "shows/Breaking Bad/S01E02.mp4",
                "shows/The Office/S02E05.mp4",
            ],
        )
        # sync_movies_cache()/list_movies() -- the paths actually served to
        # peers -- must reflect the same full set, not just the top level.
        movies_store.sync_movies_cache(self.movies_root)
        listed_paths = sorted(item["file_path"] for item in movies_store.list_movies(self.movies_root))
        self.assertEqual(listed_paths, sorted(entry.file_path for entry in entries))

    def test_scan_ignores_partial_and_lock_files(self):
        self._write("Real Movie.mp4")
        self._write("Downloading.mp4.part")
        self._write("Real Movie.mp4.lock")
        entries = movies_store.scan_movies(self.movies_root)
        self.assertEqual([entry.file_path for entry in entries], ["Real Movie.mp4"])

    def test_scan_ignores_non_video_files(self):
        # A scraper/metadata sidecar (gamelist-style XML), a poster image, an
        # .nfo release-info file, and a stray .db file must never be treated
        # as a movie -- only recognized video containers count.
        self._write("Real Movie.mp4")
        self._write("Real Movie.xml")
        self._write("Real Movie.nfo")
        self._write("poster.jpg")
        self._write("thumbs.db")
        entries = movies_store.scan_movies(self.movies_root)
        self.assertEqual([entry.file_path for entry in entries], ["Real Movie.mp4"])

    def test_scan_recognizes_common_video_containers(self):
        for name in ("A.mp4", "B.mkv", "C.avi", "D.mov", "E.webm", "F.m4v", "G.wmv", "H.flv", "I.mpg", "J.mpeg", "K.m2ts", "L.ts", "M.3gp"):
            self._write(name)
        entries = movies_store.scan_movies(self.movies_root)
        self.assertEqual(len(entries), 13)

    def test_scan_extension_match_is_case_insensitive(self):
        self._write("Real Movie.MP4")
        entries = movies_store.scan_movies(self.movies_root)
        self.assertEqual([entry.file_path for entry in entries], ["Real Movie.MP4"])

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

    def test_list_movies_page_includes_entry_key(self):
        # entry_key is the stable id the Systems/Assets page's Watch/Download
        # buttons use to build /movies/{entry_key}/stream and /download URLs.
        self._write("clips/Alpha.mp4")
        movies_store.sync_movies_cache(self.movies_root)
        page = movies_store.list_movies_page(self.movies_root)
        self.assertTrue(page["items"][0]["entry_key"])

    def test_get_movie_by_key_returns_matching_row(self):
        self._write("clips/Alpha.mp4", b"alpha-bytes")
        movies_store.sync_movies_cache(self.movies_root)
        listed = movies_store.list_movies(self.movies_root)[0]
        row = movies_store.get_movie_by_key(self.movies_root, listed["entry_key"])
        self.assertIsNotNone(row)
        self.assertEqual(row["file_path"], "clips/Alpha.mp4")
        self.assertEqual(row["entry_key"], listed["entry_key"])

    def test_get_movie_by_key_returns_none_for_unknown_key(self):
        self._write("clips/Alpha.mp4")
        movies_store.sync_movies_cache(self.movies_root)
        self.assertIsNone(movies_store.get_movie_by_key(self.movies_root, "not-a-real-key"))


class MovieMetadataStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.userdata = Path(self._tmp.name)
        self.movies_root = self.userdata / "movies"
        self.movies_root.mkdir(parents=True)
        self._db_env = os.environ.get("DRONE_STATE_DATABASE_FILE")
        os.environ["DRONE_STATE_DATABASE_FILE"] = str(self.userdata / "system" / "drone-app" / "cache.sqlite3")

    def tearDown(self):
        if self._db_env is None:
            os.environ.pop("DRONE_STATE_DATABASE_FILE", None)
        else:
            os.environ["DRONE_STATE_DATABASE_FILE"] = self._db_env
        self._tmp.cleanup()

    def test_get_movie_metadata_returns_none_when_never_scraped(self):
        self.assertIsNone(movies_store.get_movie_metadata(self.movies_root, "deadbeef"))

    def test_save_and_get_round_trips_all_fields(self):
        saved = movies_store.save_movie_metadata(
            self.movies_root,
            "deadbeef",
            provider="tmdb",
            provider_id="603",
            title="The Matrix",
            poster_relative_path="The Matrix/images/the-matrix-tmdb-image.jpg",
            backdrop_relative_path="The Matrix/images/the-matrix-tmdb-fanart.jpg",
            extra={
                "overview": "A hacker discovers reality is a simulation.",
                "genres": ["Action", "Science Fiction"],
                "cast": [{"name": "Keanu Reeves", "character": "Neo"}],
                "release_date": "1999-03-30",
                "rating": 8.2,
                "runtime_minutes": 136,
            },
        )
        self.assertEqual(saved["title"], "The Matrix")
        self.assertEqual(saved["provider"], "tmdb")
        self.assertEqual(saved["provider_id"], "603")
        self.assertEqual(saved["genres"], ["Action", "Science Fiction"])
        self.assertEqual(saved["cast"], [{"name": "Keanu Reeves", "character": "Neo"}])
        self.assertTrue(saved["scraped_at"])

        fetched = movies_store.get_movie_metadata(self.movies_root, "deadbeef")
        self.assertEqual(fetched, saved)

    def test_save_upserts_on_re_scrape(self):
        movies_store.save_movie_metadata(
            self.movies_root, "deadbeef", provider="tmdb", provider_id="1", title="Wrong Movie",
            poster_relative_path=None, backdrop_relative_path=None, extra={},
        )
        movies_store.save_movie_metadata(
            self.movies_root, "deadbeef", provider="tmdb", provider_id="603", title="The Matrix",
            poster_relative_path=None, backdrop_relative_path=None, extra={},
        )
        fetched = movies_store.get_movie_metadata(self.movies_root, "deadbeef")
        self.assertEqual(fetched["title"], "The Matrix")
        self.assertEqual(fetched["provider_id"], "603")

    def test_list_movie_display_titles_only_includes_scraped_movies(self):
        movies_store.save_movie_metadata(
            self.movies_root, "aaaa", provider="tmdb", provider_id="1", title="Scraped Movie",
            poster_relative_path=None, backdrop_relative_path=None, extra={},
        )
        titles = movies_store.list_movie_display_titles(self.movies_root)
        self.assertEqual(titles, {"aaaa": "Scraped Movie"})

    def test_list_movie_display_titles_empty_when_none_scraped(self):
        self.assertEqual(movies_store.list_movie_display_titles(self.movies_root), {})

    def test_list_movie_genres_only_includes_movies_with_genres(self):
        movies_store.save_movie_metadata(
            self.movies_root, "aaaa", provider="tmdb", provider_id="1", title="Has Genres",
            poster_relative_path=None, backdrop_relative_path=None,
            extra={"genres": ["Action", "Horror"]},
        )
        movies_store.save_movie_metadata(
            self.movies_root, "bbbb", provider="tmdb", provider_id="2", title="No Genres Key",
            poster_relative_path=None, backdrop_relative_path=None, extra={},
        )
        genres = movies_store.list_movie_genres(self.movies_root)
        self.assertEqual(genres, {"aaaa": ["Action", "Horror"]})

    def test_list_movie_genres_empty_when_none_scraped(self):
        self.assertEqual(movies_store.list_movie_genres(self.movies_root), {})

    def test_list_movie_show_titles_only_includes_scraped_episodes(self):
        movies_store.save_movie_metadata(
            self.movies_root, "aaaa", provider="tmdb_tv", provider_id="1-s1e1", title="Dexter - S01E01 - Dexter",
            poster_relative_path=None, backdrop_relative_path=None,
            extra={"media_type": "tv_episode", "show_title": "Dexter", "season_number": 1, "episode_number": 1},
        )
        movies_store.save_movie_metadata(
            self.movies_root, "bbbb", provider="tmdb", provider_id="2", title="A Plain Movie",
            poster_relative_path=None, backdrop_relative_path=None, extra={},
        )
        self.assertEqual(movies_store.list_movie_show_titles(self.movies_root), {"aaaa": "Dexter"})

    def test_list_movie_show_titles_empty_when_none_scraped(self):
        self.assertEqual(movies_store.list_movie_show_titles(self.movies_root), {})


if __name__ == "__main__":
    unittest.main()
