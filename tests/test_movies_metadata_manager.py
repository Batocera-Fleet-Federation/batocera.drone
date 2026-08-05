import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import app.storage.movie_scrape_job_items as movie_scrape_job_items
import app.storage.movie_scrape_jobs as movie_scrape_jobs
import app.storage.movies_store as movies_store
from app.common.settings import Settings
from app.movies import metadata_manager
from app.movies.tmdb_client import TmdbUnavailableError


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "MOVIES_ROOT": str(root / "movies"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "metadata-manager-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def _write_movie(root: Path, rel: str, data: bytes = b"x") -> Path:
    path = root / "movies" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class FakeTmdbClient:
    def __init__(
        self, *, details=None, search_results=None, tv_details=None,
        season_details=None, tv_episode_details=None,
    ):
        self._details = details or {}
        self._search_results = search_results or []
        self._tv_details = tv_details or {}
        self._season_details = season_details or {}
        self._tv_episode_details = tv_episode_details or {}
        self.downloaded_urls = []

    def search(self, query, year=None):
        return self._search_results

    def search_tv(self, query, year=None):
        return self._search_results

    def details(self, tmdb_id):
        return self._details

    def tv_details(self, tv_id):
        return self._tv_details

    def tv_season_details(self, tv_id, season_number):
        return self._season_details

    def tv_episode_details(self, tv_id, season_number, episode_number):
        return self._tv_episode_details

    def download_image(self, url):
        self.downloaded_urls.append(url)
        return (f"bytes-for-{url}".encode(), "image/jpeg")


_MATRIX_DETAILS = {
    "tmdb_id": 603,
    "title": "The Matrix",
    "overview": "A hacker discovers reality is a simulation.",
    "tagline": "Welcome to the Real World.",
    "genres": ["Action", "Science Fiction"],
    "cast": [{"name": "Keanu Reeves", "character": "Neo"}],
    "release_date": "1999-03-30",
    "rating": 8.2,
    "runtime_minutes": 136,
    "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
    "backdrop_url": "https://image.tmdb.org/t/p/w1280/backdrop.jpg",
}


class ScraperSettingsTests(unittest.TestCase):
    def test_defaults_to_no_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertFalse(metadata_manager.get_settings(settings)["has_api_key"])

    def test_update_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = metadata_manager.update_settings(settings, "my-tmdb-key")
            self.assertTrue(result["has_api_key"])
            self.assertTrue(metadata_manager.get_settings(settings)["has_api_key"])

    def test_rejects_blank_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                metadata_manager.update_settings(settings, "   ")

    def test_search_without_a_key_raises_tmdb_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(TmdbUnavailableError):
                metadata_manager.search(settings, "the matrix")


class SearchTests(unittest.TestCase):
    def test_uses_injected_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            fake = FakeTmdbClient(search_results=[{"tmdb_id": 603, "title": "The Matrix"}])
            results = metadata_manager.search(settings, "matrix", client=fake)
            self.assertEqual(results, [{"tmdb_id": 603, "title": "The Matrix"}])


class SearchMovieDefaultQueryTests(unittest.TestCase):
    """search_movie_default_query() backs the per-movie manual search's
    default (no custom query typed) case -- it must reuse the exact same
    candidate ladder _search_movie_with_ladder already uses for bulk
    scraping (filename_parser.search_candidates), not a separate, weaker
    cleanup. The two filenames below are real reported failures: neither
    used to scrape from the movie's own details page until the file was
    renamed by hand first, even though bulk scraping already handled them
    correctly (see BulkScrapeTests further down for that side)."""

    def test_the_terminator_paren_quality_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            fake = FakeBulkTmdbClient(match_queries={"The Terminator"})
            outcome = metadata_manager.search_movie_default_query(
                settings, "The Terminator (1080p).mp4", client=fake
            )
            self.assertEqual(fake.search_calls, ["The Terminator"])
            self.assertEqual(outcome["query"], "The Terminator")
            self.assertEqual(outcome["results"], [{"tmdb_id": 603, "title": "A Matched Movie"}])

    def test_10_cloverfield_lane_dot_separated_scene_release_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            fake = FakeBulkTmdbClient(match_queries={"10 Cloverfield Lane"})
            outcome = metadata_manager.search_movie_default_query(
                settings, "10.Cloverfield.Lane.2016.1080p.BluRay.x264-[YTS.AG].mp4", client=fake
            )
            # The year-truncated title is tried before anything noisier --
            # same rung order bulk scraping already relies on.
            self.assertEqual(fake.search_calls[0], "10 Cloverfield Lane")
            self.assertEqual(outcome["query"], "10 Cloverfield Lane (2016)")
            self.assertEqual(outcome["results"], [{"tmdb_id": 603, "title": "A Matched Movie"}])

    def test_no_match_still_returns_a_usable_label_not_the_raw_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            fake = FakeBulkTmdbClient()  # matches nothing
            outcome = metadata_manager.search_movie_default_query(
                settings, "Totally Obscure Film (1080p).mp4", client=fake
            )
            self.assertEqual(outcome["results"], [])
            self.assertEqual(outcome["query"], "Totally Obscure Film 1080p")

    def test_resolves_the_stored_api_key_when_no_client_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(TmdbUnavailableError):
                metadata_manager.search_movie_default_query(settings, "The Terminator (1080p).mp4")


class ApplyTests(unittest.TestCase):
    def test_unknown_movie_raises_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(metadata_manager.MovieNotFoundError):
                metadata_manager.apply(settings, "not-a-real-key", 603, client=FakeTmdbClient(details=_MATRIX_DETAILS))

    def test_downloads_artwork_and_saves_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "The Matrix (1999).mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            fake = FakeTmdbClient(details=_MATRIX_DETAILS)
            result = metadata_manager.apply(settings, entry_key, 603, client=fake)

            self.assertEqual(result["title"], "The Matrix")
            self.assertEqual(result["provider"], "tmdb")
            self.assertEqual(result["provider_id"], "603")
            self.assertEqual(result["genres"], ["Action", "Science Fiction"])
            self.assertEqual(len(fake.downloaded_urls), 2)

            poster_path = root / "movies" / result["poster_relative_path"]
            backdrop_path = root / "movies" / result["backdrop_relative_path"]
            self.assertTrue(poster_path.is_file())
            self.assertTrue(backdrop_path.is_file())
            # Artwork lands in an images/ folder sibling to the movie file.
            self.assertEqual(poster_path.parent.name, "images")
            self.assertEqual(poster_path.parent.parent.resolve(), (root / "movies").resolve())
            # Named after the movie, ROM-artwork-style: <safe-stem>-tmdb-<field>.jpg
            # (non-alphanumeric characters sanitized to "-", same as ROM scraped art).
            self.assertEqual(poster_path.name, "The-Matrix-1999--tmdb-image.jpg")
            self.assertEqual(backdrop_path.name, "The-Matrix-1999--tmdb-fanart.jpg")

            stored = movies_store.get_movie_metadata(settings.movies_root, entry_key)
            self.assertEqual(stored["title"], "The Matrix")

    def test_no_poster_or_backdrop_url_skips_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Untitled.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            details = dict(_MATRIX_DETAILS)
            details["poster_url"] = None
            details["backdrop_url"] = None
            fake = FakeTmdbClient(details=details)
            result = metadata_manager.apply(settings, entry_key, 603, client=fake)
            self.assertIsNone(result["poster_relative_path"])
            self.assertIsNone(result["backdrop_relative_path"])
            self.assertEqual(fake.downloaded_urls, [])

    def test_artwork_for_same_basename_in_different_folders_does_not_collide(self):
        # Two different shows both happen to have an "S01E01" episode --
        # the images/ folder is a sibling of the *specific* episode file, so
        # this must not overwrite one show's art with the other's.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Shows/Breaking Bad/S01E01.mp4")
            _write_movie(root, "Shows/The Office/S01E01.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            movies = {m["file_path"]: m["entry_key"] for m in movies_store.list_movies(settings.movies_root)}

            details_a = dict(_MATRIX_DETAILS, title="Breaking Bad S01E01")
            details_b = dict(_MATRIX_DETAILS, title="The Office S01E01")
            metadata_manager.apply(settings, movies["Shows/Breaking Bad/S01E01.mp4"], 1, client=FakeTmdbClient(details=details_a))
            metadata_manager.apply(settings, movies["Shows/The Office/S01E01.mp4"], 2, client=FakeTmdbClient(details=details_b))

            bb_meta = movies_store.get_movie_metadata(settings.movies_root, movies["Shows/Breaking Bad/S01E01.mp4"])
            office_meta = movies_store.get_movie_metadata(settings.movies_root, movies["Shows/The Office/S01E01.mp4"])
            self.assertEqual(bb_meta["title"], "Breaking Bad S01E01")
            self.assertEqual(office_meta["title"], "The Office S01E01")
            self.assertNotEqual(bb_meta["poster_relative_path"], office_meta["poster_relative_path"])
            self.assertIn("Breaking Bad", bb_meta["poster_relative_path"])
            self.assertIn("The Office", office_meta["poster_relative_path"])


class ApplyTvEpisodeTests(unittest.TestCase):
    def test_unknown_movie_raises_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(metadata_manager.MovieNotFoundError):
                metadata_manager.apply_tv_episode(
                    settings, "not-a-real-key", 1405, 1, 1,
                    show_details=dict(_MATRIX_DETAILS, title="Dexter"),
                    client=FakeTmdbClient(),
                )

    def test_prefers_season_poster_over_show_poster(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Dexter (2006) - S01E01 - Dexter.mkv")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            fake = FakeTmdbClient(
                tv_details=dict(_MATRIX_DETAILS, title="Dexter", poster_url="https://image.tmdb.org/t/p/w500/show-poster.jpg"),
                season_details={"title": "Season 1", "overview": "", "air_date": None, "poster_url": "https://image.tmdb.org/t/p/w500/season1-poster.jpg"},
                tv_episode_details={"title": "Dexter", "overview": "", "air_date": None, "rating": None, "still_url": None},
            )
            metadata_manager.apply_tv_episode(settings, entry_key, 1405, 1, 1, client=fake)
            self.assertIn("https://image.tmdb.org/t/p/w500/season1-poster.jpg", fake.downloaded_urls)
            self.assertNotIn("https://image.tmdb.org/t/p/w500/show-poster.jpg", fake.downloaded_urls)

    def test_falls_back_to_show_poster_when_season_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Dexter (2006) - S01E01 - Dexter.mkv")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            fake = FakeTmdbClient(
                tv_details=dict(_MATRIX_DETAILS, title="Dexter", poster_url="https://image.tmdb.org/t/p/w500/show-poster.jpg"),
                season_details={"title": "Season 1", "overview": "", "air_date": None, "poster_url": None},
                tv_episode_details={"title": "Dexter", "overview": "", "air_date": None, "rating": None, "still_url": None},
            )
            metadata_manager.apply_tv_episode(settings, entry_key, 1405, 1, 1, client=fake)
            self.assertIn("https://image.tmdb.org/t/p/w500/show-poster.jpg", fake.downloaded_urls)

    def test_stores_season_name_and_overview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Dexter (2006) - S01E01 - Dexter.mkv")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            fake = FakeTmdbClient(
                tv_details=dict(_MATRIX_DETAILS, title="Dexter"),
                season_details={"title": "Season 1", "overview": "Dexter's first season.", "air_date": "2006-10-01", "poster_url": None},
                tv_episode_details={"title": "Dexter", "overview": "Pilot.", "air_date": None, "rating": None, "still_url": None},
            )
            result = metadata_manager.apply_tv_episode(settings, entry_key, 1405, 1, 1, client=fake)
            self.assertEqual(result["season_name"], "Season 1")
            self.assertEqual(result["season_overview"], "Dexter's first season.")
            # Episode's own overview is unaffected by the season's.
            self.assertEqual(result["overview"], "Pilot.")

    def test_season_details_param_skips_refetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Dexter (2006) - S01E01 - Dexter.mkv")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            class NoSeasonCallClient(FakeTmdbClient):
                def tv_season_details(self, tv_id, season_number):
                    raise AssertionError("must not call tv_season_details when season_details is already given")

            fake = NoSeasonCallClient(tv_details=dict(_MATRIX_DETAILS, title="Dexter"))
            metadata_manager.apply_tv_episode(
                settings, entry_key, 1405, 1, 1,
                season_details={"title": "Season 1", "overview": "", "air_date": None, "poster_url": None},
                client=fake,
            )


class FakeBulkTmdbClient:
    """Unlike FakeTmdbClient above (fixed details, fixed results), this fake
    varies its response by query -- the bulk job searches a different query
    per movie, so a single canned response can't exercise matched/failed
    counting the way the per-movie tests need."""

    def __init__(
        self, *, match_queries=None, unavailable_after=None, search_delay_seconds=0,
        tv_match_queries=None, tv_details=None, season_details=None, tv_episode_details=None,
    ):
        self._match_queries = match_queries if match_queries is not None else set()
        self._tv_match_queries = tv_match_queries if tv_match_queries is not None else set()
        self._unavailable_after = unavailable_after
        self._search_delay_seconds = search_delay_seconds
        self._tv_details = tv_details or dict(_MATRIX_DETAILS, title="A Matched Show")
        # No poster_url by default -- apply_tv_episode falls back to the show
        # poster, which is what every test written before season-level
        # posters existed already asserts on.
        self._season_details = season_details or {"title": "", "overview": "", "air_date": None, "poster_url": None}
        self._tv_episode_details = tv_episode_details or {"title": "A Matched Episode", "overview": "", "air_date": None, "rating": None, "still_url": None}
        self.search_calls = []
        self.search_tv_calls = []
        self.tv_details_calls = []
        self.tv_season_details_calls = []
        self.tv_episode_details_calls = []

    def _maybe_raise_unavailable(self, call_count):
        if self._unavailable_after is not None and call_count > self._unavailable_after:
            raise TmdbUnavailableError("TMDb rejected the configured API key")

    def search(self, query, year=None):
        if self._search_delay_seconds:
            time.sleep(self._search_delay_seconds)
        self.search_calls.append(query)
        self._maybe_raise_unavailable(len(self.search_calls) + len(self.search_tv_calls))
        if query in self._match_queries:
            return [{"tmdb_id": 603, "title": "A Matched Movie"}]
        return []

    def search_tv(self, query, year=None):
        self.search_tv_calls.append(query)
        self._maybe_raise_unavailable(len(self.search_calls) + len(self.search_tv_calls))
        if query in self._tv_match_queries:
            return [{"tmdb_id": 909, "title": "A Matched Show"}]
        return []

    def details(self, tmdb_id):
        return dict(_MATRIX_DETAILS, tmdb_id=tmdb_id)

    def tv_details(self, tv_id):
        self.tv_details_calls.append(tv_id)
        return dict(self._tv_details, tmdb_id=tv_id)

    def tv_season_details(self, tv_id, season_number):
        self.tv_season_details_calls.append((tv_id, season_number))
        return dict(self._season_details)

    def tv_episode_details(self, tv_id, season_number, episode_number):
        self.tv_episode_details_calls.append((tv_id, season_number, episode_number))
        return dict(self._tv_episode_details)

    def download_image(self, url):
        return (b"fake-image-bytes", "image/jpeg")


def _wait_for_bulk_scrape_status(settings, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = metadata_manager.get_bulk_scrape_status(settings)
        if job and job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"bulk scrape job still running after {timeout}s")


class BulkScrapeTests(unittest.TestCase):
    def setUp(self):
        # Real per-candidate throttling (see _throttle_before_tmdb_call) has
        # no test value at real-time speed -- zero it out everywhere in this
        # class rather than eating it in every test.
        patcher = mock.patch("app.movies.metadata_manager._REQUEST_THROTTLE_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_job_yet_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertIsNone(metadata_manager.get_bulk_scrape_status(settings))

    def test_rejects_concurrent_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            movie_scrape_jobs.create_running(settings, rescan_all=False, total=1)
            result = metadata_manager.start_bulk_scrape(settings, client=FakeBulkTmdbClient())
            self.assertEqual(result["status"], "already_running")

    def test_two_truly_simultaneous_starts_only_let_one_through(self):
        # Regression test: any_running()-check-then-create_running()-insert
        # is two separate SQLite operations, so two requests arriving on
        # different threads at almost the same instant could both observe
        # "nothing running yet" before either had inserted its row --
        # _BULK_SCRAPE_START_LOCK exists specifically to close that window.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Some Movie.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            results = []
            barrier = threading.Barrier(2)

            def _start():
                barrier.wait(timeout=2)
                # A tiny per-search delay keeps the winning job's background
                # thread genuinely "running" past the barrier, so the losing
                # call's any_running() check has something real to observe --
                # without this, an instant fake client can let job 1 finish
                # before job 2 even reaches the lock, making both legitimately "ok".
                results.append(
                    metadata_manager.start_bulk_scrape(settings, client=FakeBulkTmdbClient(search_delay_seconds=0.3))
                )

            threads = [threading.Thread(target=_start) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            statuses = sorted(r["status"] for r in results)
            self.assertEqual(statuses, ["already_running", "ok"])

            # Let the winner's own background (daemon) thread finish before
            # the tempdir goes away -- otherwise it tries to write to the
            # now-deleted SQLite file after this block exits.
            _wait_for_bulk_scrape_status(settings)

    def test_no_api_key_returns_error_without_creating_a_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Some Movie.mp4")
            settings = _build_settings(root)
            result = metadata_manager.start_bulk_scrape(settings)
            self.assertEqual(result["status"], "error")
            self.assertIsNone(metadata_manager.get_bulk_scrape_status(settings))

    def test_default_only_scrapes_movies_missing_a_poster(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Already Scraped.mp4")
            _write_movie(root, "Needs Scraping.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            by_name = {m["movie_name"]: m["entry_key"] for m in movies_store.list_movies(settings.movies_root)}

            metadata_manager.apply(
                settings, by_name["Already Scraped.mp4"], 1,
                client=FakeTmdbClient(details=_MATRIX_DETAILS),
            )

            fake = FakeBulkTmdbClient(match_queries={"Needs Scraping"})
            result = metadata_manager.start_bulk_scrape(settings, rescan_all=False, client=fake)
            self.assertEqual(result["status"], "ok")
            job = _wait_for_bulk_scrape_status(settings)

            self.assertEqual(job["status"], "complete")
            self.assertEqual(job["total"], 1)
            self.assertEqual(fake.search_calls, ["Needs Scraping"])
            self.assertEqual(job["matched_count"], 1)

            # apply() takes the title from client.details(), not the search
            # result -- FakeBulkTmdbClient.details() always returns
            # _MATRIX_DETAILS regardless of which movie matched.
            scraped = movies_store.get_movie_metadata(settings.movies_root, by_name["Needs Scraping.mp4"])
            self.assertEqual(scraped["title"], "The Matrix")

    def test_rescan_all_scrapes_every_movie_including_already_scraped_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Already Scraped.mp4")
            _write_movie(root, "Needs Scraping.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            by_name = {m["movie_name"]: m["entry_key"] for m in movies_store.list_movies(settings.movies_root)}

            metadata_manager.apply(
                settings, by_name["Already Scraped.mp4"], 1,
                client=FakeTmdbClient(details=_MATRIX_DETAILS),
            )

            fake = FakeBulkTmdbClient(match_queries={"Needs Scraping", "Already Scraped"})
            result = metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=fake)
            self.assertEqual(result["status"], "ok")
            job = _wait_for_bulk_scrape_status(settings)

            self.assertEqual(job["total"], 2)
            self.assertCountEqual(fake.search_calls, ["Needs Scraping", "Already Scraped"])
            self.assertEqual(job["matched_count"], 2)

    def test_counts_matched_skipped_and_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Good Match.mp4")
            _write_movie(root, "No Match.mp4")
            _write_movie(root, "----.mp4")  # cleans to an empty query -- skipped, not searched
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            fake = FakeBulkTmdbClient(match_queries={"Good Match"})
            result = metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=fake)
            self.assertEqual(result["status"], "ok")
            job = _wait_for_bulk_scrape_status(settings)

            self.assertEqual(job["status"], "complete")
            self.assertEqual(job["total"], 3)
            self.assertEqual(job["processed"], 3)
            self.assertEqual(job["matched_count"], 1)
            self.assertEqual(job["skipped_count"], 1)
            self.assertEqual(job["failed_count"], 1)
            self.assertNotIn("", fake.search_calls)

    def test_per_item_results_are_recorded_with_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Good Match.mp4")
            _write_movie(root, "No Match.mp4")
            _write_movie(root, "----.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            by_name = {m["movie_name"]: m["entry_key"] for m in movies_store.list_movies(settings.movies_root)}

            fake = FakeBulkTmdbClient(match_queries={"Good Match"})
            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=fake)
            _wait_for_bulk_scrape_status(settings)

            matched = movie_scrape_job_items.list_by_status(settings, movie_scrape_job_items.STATUS_MATCHED)
            self.assertEqual([i["entry_key"] for i in matched["items"]], [by_name["Good Match.mp4"]])

            failed = movie_scrape_job_items.list_by_status(settings, movie_scrape_job_items.STATUS_FAILED)
            self.assertEqual(failed["items"][0]["entry_key"], by_name["No Match.mp4"])
            self.assertIn("no TMDb results", failed["items"][0]["reason"])

            skipped = movie_scrape_job_items.list_by_status(settings, movie_scrape_job_items.STATUS_SKIPPED)
            self.assertEqual(skipped["items"][0]["entry_key"], by_name["----.mp4"])

    def test_extras_are_recorded_as_skipped_with_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Shows/Dexter/Dexter (2006) S01/Featurettes/Blood Splatter 101.mkv")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=FakeBulkTmdbClient())
            _wait_for_bulk_scrape_status(settings)

            skipped = movie_scrape_job_items.list_by_status(settings, movie_scrape_job_items.STATUS_SKIPPED)
            self.assertEqual(skipped["total"], 1)
            self.assertIn("extra", skipped["items"][0]["reason"])

    def test_mid_job_tmdb_unavailable_records_every_remaining_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "First Movie.mp4")
            _write_movie(root, "Second Movie.mp4")
            _write_movie(root, "Third Movie.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            fake = FakeBulkTmdbClient(match_queries=set(), unavailable_after=0)
            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=fake)
            _wait_for_bulk_scrape_status(settings)

            failed = movie_scrape_job_items.list_by_status(settings, movie_scrape_job_items.STATUS_FAILED)
            self.assertEqual(failed["total"], 3)
            self.assertTrue(all("rate-limit" in i["reason"] or "rejected" in i["reason"] for i in failed["items"]))

    def test_a_fresh_run_clears_the_previous_runs_item_breakdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Good Match.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=FakeBulkTmdbClient(match_queries={"Good Match"}))
            _wait_for_bulk_scrape_status(settings)
            self.assertEqual(movie_scrape_job_items.list_by_status(settings, movie_scrape_job_items.STATUS_MATCHED)["total"], 1)

            _write_movie(root, "No Match.mp4")
            movies_store.sync_movies_cache(settings.movies_root)
            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=FakeBulkTmdbClient())
            _wait_for_bulk_scrape_status(settings)
            # The first run's "Good Match" matched-row is gone -- a fresh
            # full run's breakdown replaces the previous one, not merges.
            self.assertEqual(movie_scrape_job_items.list_by_status(settings, movie_scrape_job_items.STATUS_MATCHED)["total"], 0)

    def test_retry_failed_rescopes_to_just_the_failed_set_and_updates_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Good Match.mp4")
            _write_movie(root, "Retry Me.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            by_name = {m["movie_name"]: m["entry_key"] for m in movies_store.list_movies(settings.movies_root)}

            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=FakeBulkTmdbClient(match_queries={"Good Match"}))
            _wait_for_bulk_scrape_status(settings)
            self.assertEqual(movie_scrape_job_items.list_by_status(settings, movie_scrape_job_items.STATUS_FAILED)["total"], 1)

            # Simulates the underlying problem (e.g. rate-limiting) having
            # cleared: this time the retried title matches.
            retry_client = FakeBulkTmdbClient(match_queries={"Retry Me"})
            result = metadata_manager.retry_bulk_scrape_items(settings, status="failed", client=retry_client)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["job"]["total"], 1)
            _wait_for_bulk_scrape_status(settings)

            self.assertEqual(retry_client.search_calls, ["Retry Me"])
            self.assertEqual(movie_scrape_job_items.list_by_status(settings, movie_scrape_job_items.STATUS_FAILED)["total"], 0)
            matched = movie_scrape_job_items.list_by_status(settings, movie_scrape_job_items.STATUS_MATCHED)
            self.assertCountEqual([i["entry_key"] for i in matched["items"]], [by_name["Good Match.mp4"], by_name["Retry Me.mp4"]])

    def test_retry_specific_entry_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "No Match.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=FakeBulkTmdbClient())
            _wait_for_bulk_scrape_status(settings)

            retry_client = FakeBulkTmdbClient(match_queries={"No Match"})
            result = metadata_manager.retry_bulk_scrape_items(settings, entry_keys=[entry_key], client=retry_client)
            self.assertEqual(result["job"]["total"], 1)
            _wait_for_bulk_scrape_status(settings)
            matched = movie_scrape_job_items.list_by_status(settings, movie_scrape_job_items.STATUS_MATCHED)
            self.assertEqual([i["entry_key"] for i in matched["items"]], [entry_key])

    def test_get_bulk_scrape_items_wraps_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "No Match.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=FakeBulkTmdbClient())
            _wait_for_bulk_scrape_status(settings)

            page = metadata_manager.get_bulk_scrape_items(settings, "failed")
            self.assertEqual(page["total"], 1)

    def test_year_bearing_scene_release_name_is_searched_with_year_filter_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "28.Days.Later.2002.1080p.BluRay.DDP5.1.x265.10bit-GalaxyRG265.mkv")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            fake = FakeBulkTmdbClient(match_queries={"28 Days Later"})
            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=fake)
            job = _wait_for_bulk_scrape_status(settings)

            self.assertEqual(job["matched_count"], 1)
            # The year-truncated title is tried before anything noisier.
            self.assertEqual(fake.search_calls[0], "28 Days Later")

    def test_hyphenated_title_is_not_mangled_by_the_group_tag_stripper(self):
        # Regression: an early version of the release-group stripper matched
        # any "-word..." run to end of string, which ate "-Man" off
        # "Ant-Man" -- see filename_parser.search_candidates's docstring.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Ant-Man (1080p).mkv")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            fake = FakeBulkTmdbClient(match_queries={"Ant Man"})
            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=fake)
            job = _wait_for_bulk_scrape_status(settings)

            self.assertEqual(job["matched_count"], 1)

    def test_extras_folder_content_is_skipped_without_a_tmdb_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Shows/Dexter/Dexter (2006) S01/Featurettes/Blood Splatter 101.mkv")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            fake = FakeBulkTmdbClient()
            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=fake)
            job = _wait_for_bulk_scrape_status(settings)

            self.assertEqual(job["skipped_count"], 1)
            self.assertEqual(job["matched_count"], 0)
            self.assertEqual(job["failed_count"], 0)
            self.assertEqual(fake.search_calls, [])
            self.assertEqual(fake.search_tv_calls, [])

    def test_tv_episode_is_searched_via_search_tv_and_saved_as_an_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(
                root,
                "Shows/Dexter/Dexter (2006) S01/Dexter (2006) - S01E01 - Dexter (1080p BluRay x265 Silence).mkv",
            )
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            fake = FakeBulkTmdbClient(
                tv_match_queries={"Dexter"},
                tv_details=dict(_MATRIX_DETAILS, title="Dexter", tmdb_id=909),
                tv_episode_details={
                    "title": "Dexter", "overview": "Pilot overview", "air_date": "2006-10-01",
                    "rating": 8.0, "still_url": None,
                },
            )
            result = metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=fake)
            self.assertEqual(result["status"], "ok")
            job = _wait_for_bulk_scrape_status(settings)

            self.assertEqual(job["matched_count"], 1)
            self.assertEqual(fake.search_calls, [])
            self.assertEqual(fake.search_tv_calls, ["Dexter"])
            self.assertEqual(len(fake.tv_details_calls), 1)

            scraped = movies_store.get_movie_metadata(settings.movies_root, entry_key)
            self.assertEqual(scraped["provider"], "tmdb_tv")
            self.assertEqual(scraped["title"], "Dexter - S01E01 - Dexter")
            self.assertEqual(scraped["media_type"], "tv_episode")
            self.assertEqual(scraped["show_title"], "Dexter")
            self.assertEqual(scraped["season_number"], 1)
            self.assertEqual(scraped["episode_number"], 1)

    def test_multiple_episodes_of_the_same_show_only_search_and_fetch_show_details_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Shows/Dexter/Dexter (2006) S01/Dexter (2006) - S01E01 - Dexter.mkv")
            _write_movie(root, "Shows/Dexter/Dexter (2006) S01/Dexter (2006) - S01E02 - Crocodile.mkv")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            fake = FakeBulkTmdbClient(tv_match_queries={"Dexter"})
            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=fake)
            job = _wait_for_bulk_scrape_status(settings)

            self.assertEqual(job["matched_count"], 2)
            self.assertEqual(fake.search_tv_calls, ["Dexter"])
            self.assertEqual(len(fake.tv_details_calls), 1)
            self.assertEqual(len(fake.tv_season_details_calls), 1)
            self.assertEqual(len(fake.tv_episode_details_calls), 2)

    def test_episodes_of_different_seasons_each_fetch_their_own_season_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Shows/Dexter/Dexter (2006) S01/Dexter (2006) - S01E01 - Dexter.mkv")
            _write_movie(root, "Shows/Dexter/Dexter (2006) S02/Dexter (2006) - S02E01 - It's Alive!.mkv")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            fake = FakeBulkTmdbClient(tv_match_queries={"Dexter"})
            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=fake)
            job = _wait_for_bulk_scrape_status(settings)

            self.assertEqual(job["matched_count"], 2)
            # One show search/details fetch shared across both seasons, but
            # a season-details fetch per distinct (show, season) pair.
            self.assertEqual(len(fake.tv_details_calls), 1)
            self.assertCountEqual(fake.tv_season_details_calls, [(909, 1), (909, 2)])

    def test_show_not_found_on_tmdb_counts_as_failed_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Shows/Unknown Show/Unknown Show (2020) - S01E01 - Pilot.mkv")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            fake = FakeBulkTmdbClient()  # no tv_match_queries -- search_tv returns []
            metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=fake)
            job = _wait_for_bulk_scrape_status(settings)

            self.assertEqual(job["failed_count"], 1)
            self.assertEqual(job["matched_count"], 0)

    def test_throttles_before_each_tmdb_touching_candidate(self):
        # Uses the real (non-zeroed) module throttle constant for this one
        # test -- everything else in this class patches it to 0 for speed.
        # time.sleep is a process-wide singleton, and _wait_for_bulk_scrape_status's
        # own polling loop also calls it (with a different, smaller delay) while
        # this test's background job thread runs -- so this counts only the
        # throttle's own 0.2s calls rather than asserting a single total call.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Good Match.mp4")
            _write_movie(root, "----.mp4")  # empty query -- never touches TMDb, never throttled
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            fake = FakeBulkTmdbClient(match_queries={"Good Match"})
            with mock.patch("app.movies.metadata_manager._REQUEST_THROTTLE_SECONDS", 0.2):
                with mock.patch("app.movies.metadata_manager.time.sleep") as sleep_mock:
                    metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=fake)
                    job = _wait_for_bulk_scrape_status(settings)
            self.assertEqual(job["matched_count"], 1)
            throttle_calls = [c for c in sleep_mock.call_args_list if c.args == (0.2,)]
            self.assertEqual(len(throttle_calls), 1)

    def test_tmdb_becoming_unavailable_mid_job_stops_early_without_erroring_the_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "First Movie.mp4")
            _write_movie(root, "Second Movie.mp4")
            _write_movie(root, "Third Movie.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)

            # Rejects the key on the very first search call.
            fake = FakeBulkTmdbClient(match_queries=set(), unavailable_after=0)
            result = metadata_manager.start_bulk_scrape(settings, rescan_all=True, client=fake)
            self.assertEqual(result["status"], "ok")
            job = _wait_for_bulk_scrape_status(settings)

            self.assertEqual(job["status"], "complete")
            self.assertEqual(job["processed"], 3)
            self.assertEqual(job["failed_count"], 3)
            self.assertEqual(job["matched_count"], 0)
            # Stopped after the first rejected call rather than retrying it
            # for every remaining movie.
            self.assertEqual(len(fake.search_calls), 1)


if __name__ == "__main__":
    unittest.main()
