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

    def search(self, query):
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


if __name__ == "__main__":
    unittest.main()
