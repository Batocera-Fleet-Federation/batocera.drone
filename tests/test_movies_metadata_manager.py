import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        "SHOWS_ROOT": str(root / "shows"),
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
    def __init__(self, *, details=None, search_results=None):
        self._details = details or {}
        self._search_results = search_results or []
        self.downloaded_urls = []

    def search(self, query, year=None):
        return self._search_results

    def details(self, tmdb_id):
        return self._details

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


class MovieFolderNameTests(unittest.TestCase):
    def test_returns_the_immediate_parent_directory_name(self):
        movie = {"file_path": "Alien Resurrection (1997)/Alien.Resurrection.1997.mkv"}
        self.assertEqual(metadata_manager.movie_folder_name(movie), "Alien Resurrection (1997)")

    def test_bare_file_with_no_parent_returns_none(self):
        movie = {"file_path": "Alien.Resurrection.1997.mkv"}
        self.assertIsNone(metadata_manager.movie_folder_name(movie))

    def test_falls_back_to_relative_path_when_file_path_is_absent(self):
        movie = {"relative_path": "Some Folder (2001)/movie.mkv"}
        self.assertEqual(metadata_manager.movie_folder_name(movie), "Some Folder (2001)")


class FakeQueryTmdbClient:
    """Matches only the queries in ``match_queries`` and records every search,
    so the default-query ladder's rung order can be asserted."""

    def __init__(self, *, match_queries=None):
        self._match_queries = match_queries if match_queries is not None else set()
        self.search_calls = []

    def search(self, query, year=None):
        self.search_calls.append(query)
        if query in self._match_queries:
            return [{"tmdb_id": 603, "title": "A Matched Movie"}]
        return []


class SearchMovieDefaultQueryTests(unittest.TestCase):
    """search_movie_default_query() backs the per-movie manual search's
    default (no custom query typed) case -- it must use the candidate ladder
    (filename_parser.search_candidates), not a separate, weaker cleanup. The
    two filenames below are real reported failures: neither used to scrape
    from the movie's own details page until the file was renamed by hand
    first."""

    def test_the_terminator_paren_quality_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            fake = FakeQueryTmdbClient(match_queries={"The Terminator"})
            outcome = metadata_manager.search_movie_default_query(
                settings, "The Terminator (1080p).mp4", client=fake
            )
            self.assertEqual(fake.search_calls, ["The Terminator"])
            self.assertEqual(outcome["query"], "The Terminator")
            self.assertEqual(outcome["results"], [{"tmdb_id": 603, "title": "A Matched Movie"}])

    def test_10_cloverfield_lane_dot_separated_scene_release_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            fake = FakeQueryTmdbClient(match_queries={"10 Cloverfield Lane"})
            outcome = metadata_manager.search_movie_default_query(
                settings, "10.Cloverfield.Lane.2016.1080p.BluRay.x264-[YTS.AG].mp4", client=fake
            )
            # The year-truncated title is tried before anything noisier.
            self.assertEqual(fake.search_calls[0], "10 Cloverfield Lane")
            self.assertEqual(outcome["query"], "10 Cloverfield Lane (2016)")
            self.assertEqual(outcome["results"], [{"tmdb_id": 603, "title": "A Matched Movie"}])

    def test_no_match_still_returns_a_usable_label_not_the_raw_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            fake = FakeQueryTmdbClient()  # matches nothing
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
            # Plex flat-library artwork is named after the adjacent movie.
            self.assertEqual(poster_path.name, "The Matrix (1999).jpg")
            self.assertEqual(backdrop_path.name, "The Matrix (1999)-fanart.jpg")

            stored = movies_store.get_movie_metadata(settings.movies_root, entry_key)
            self.assertEqual(stored["title"], "The Matrix")

    def test_folder_movie_artwork_uses_plex_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "The Matrix (1999)/The Matrix (1999).mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root, settings.shows_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            result = metadata_manager.apply(settings, entry_key, 603, client=FakeTmdbClient(details=_MATRIX_DETAILS))

            self.assertEqual(result["poster_relative_path"], "The Matrix (1999)/poster.jpg")
            self.assertEqual(result["backdrop_relative_path"], "The Matrix (1999)/fanart.jpg")

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

    def test_youtube_trailer_key_is_stored_on_extra(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "The Matrix (1999).mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            details = dict(_MATRIX_DETAILS, youtube_trailer_key="m8e-FF8MsqU")
            result = metadata_manager.apply(settings, entry_key, 603, client=FakeTmdbClient(details=details))
            self.assertEqual(result["youtube_trailer_key"], "m8e-FF8MsqU")

            stored = movies_store.get_movie_metadata(settings.movies_root, entry_key)
            self.assertEqual(stored["youtube_trailer_key"], "m8e-FF8MsqU")

    def test_no_trailer_key_stores_none_not_a_missing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "The Matrix (1999).mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            result = metadata_manager.apply(settings, entry_key, 603, client=FakeTmdbClient(details=_MATRIX_DETAILS))
            self.assertIsNone(result["youtube_trailer_key"])


class ApplyByReferenceTests(unittest.TestCase):
    """apply_by_reference() is the direct-lookup escape hatch for a movie
    whose title search doesn't reliably surface it -- a human pastes a
    themoviedb.org movie URL (or bare id) they found by searching TMDb's own
    site directly. Real live case this was built for: "Hell of the Dead"
    (an AKA of TMDb id 21380, "Night of the Zombies" / "Virus") not coming
    up under that title in this app's own TMDb search."""

    def test_bare_id_applies_the_same_as_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Hell of the Dead.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            fake = FakeTmdbClient(details=_MATRIX_DETAILS)
            result = metadata_manager.apply_by_reference(settings, entry_key, "603", client=fake)
            self.assertEqual(result["title"], "The Matrix")
            self.assertEqual(result["provider_id"], "603")

    def test_full_tmdb_url_applies_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Hell of the Dead.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            details = dict(_MATRIX_DETAILS, tmdb_id=21380, title="Night of the Zombies")
            fake = FakeTmdbClient(details=details)
            result = metadata_manager.apply_by_reference(
                settings, entry_key, "https://www.themoviedb.org/movie/21380-virus?language=da-DK", client=fake,
            )
            self.assertEqual(result["title"], "Night of the Zombies")
            self.assertEqual(result["provider_id"], "21380")

    def test_unparseable_reference_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Hell of the Dead.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            with self.assertRaises(ValueError):
                metadata_manager.apply_by_reference(
                    settings, entry_key, "not a tmdb link", client=FakeTmdbClient(details=_MATRIX_DETAILS),
                )

    def test_unknown_movie_still_raises_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(metadata_manager.MovieNotFoundError):
                metadata_manager.apply_by_reference(
                    settings, "not-a-real-key", "21380", client=FakeTmdbClient(details=_MATRIX_DETAILS),
                )


class DeleteMetadataTests(unittest.TestCase):
    def test_never_scraped_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "The Matrix (1999).mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            self.assertEqual(metadata_manager.delete_metadata(settings, entry_key), {"deleted": False})

    def test_removes_the_metadata_row_and_the_artwork_files_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "The Matrix (1999).mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            fake = FakeTmdbClient(details=_MATRIX_DETAILS)
            applied = metadata_manager.apply(settings, entry_key, 603, client=fake)
            poster_path = root / "movies" / applied["poster_relative_path"]
            backdrop_path = root / "movies" / applied["backdrop_relative_path"]
            self.assertTrue(poster_path.is_file())
            self.assertTrue(backdrop_path.is_file())

            result = metadata_manager.delete_metadata(settings, entry_key)

            self.assertEqual(result, {"deleted": True})
            self.assertIsNone(movies_store.get_movie_metadata(settings.movies_root, entry_key))
            self.assertFalse(poster_path.exists())
            self.assertFalse(backdrop_path.exists())
            # The movie file itself is untouched -- only the scraped
            # metadata/artwork was ever meant to go away.
            self.assertTrue((root / "movies" / "The Matrix (1999).mp4").is_file())

    def test_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "The Matrix (1999).mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]
            metadata_manager.apply(settings, entry_key, 603, client=FakeTmdbClient(details=_MATRIX_DETAILS))

            self.assertEqual(metadata_manager.delete_metadata(settings, entry_key), {"deleted": True})
            # A second delete finds nothing left to remove -- must not raise
            # (e.g. on the already-unlinked artwork files).
            self.assertEqual(metadata_manager.delete_metadata(settings, entry_key), {"deleted": False})

    def test_missing_artwork_files_on_disk_do_not_raise(self):
        # The stored relative paths can outlive the files themselves (a
        # human manually cleared the images/ folder, a prior partial
        # failure, ...) -- deleting the row must not depend on the files
        # still being there.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "The Matrix (1999).mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]
            metadata_manager.apply(settings, entry_key, 603, client=FakeTmdbClient(details=_MATRIX_DETAILS))

            for image in (root / "movies").rglob("*.jpg"):
                image.unlink()

            self.assertEqual(metadata_manager.delete_metadata(settings, entry_key), {"deleted": True})


class DeleteMovieTests(unittest.TestCase):
    """delete_movie() -- unlike delete_metadata(), this removes the movie's
    file itself too (plus any scraped metadata/artwork), for the Movies UI
    detail page's delete action."""

    def test_deletes_the_file_and_any_scraped_metadata_and_artwork(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movie_path = _write_movie(root, "The Matrix (1999).mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]
            applied = metadata_manager.apply(settings, entry_key, 603, client=FakeTmdbClient(details=_MATRIX_DETAILS))
            poster_path = root / "movies" / applied["poster_relative_path"]
            self.assertTrue(poster_path.is_file())

            result = metadata_manager.delete_movie(settings, entry_key)

            self.assertEqual(result, {"deleted": True, "file_path": "The Matrix (1999).mp4"})
            self.assertFalse(movie_path.exists())
            self.assertFalse(poster_path.exists())
            self.assertIsNone(movies_store.get_movie_metadata(settings.movies_root, entry_key))
            self.assertIsNone(movies_store.get_movie_by_key(settings.movies_root, entry_key))

    def test_deletes_a_never_scraped_movie_file_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movie_path = _write_movie(root, "Unscraped.mp4")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]

            result = metadata_manager.delete_movie(settings, entry_key)

            self.assertEqual(result, {"deleted": True, "file_path": "Unscraped.mp4"})
            self.assertFalse(movie_path.exists())

    def test_unknown_entry_key_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertEqual(metadata_manager.delete_movie(settings, "not-a-real-key"), {"deleted": False})


if __name__ == "__main__":
    unittest.main()
