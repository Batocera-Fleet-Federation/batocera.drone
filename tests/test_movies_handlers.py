"""HTTP-level tests for HandlersMoviesMixin: listing, path-traversal safety,
download, and the Range-aware stream endpoint used by the Systems/Assets
page's <video> player. Movies scanning/inventory itself is covered in
test_movies_store.py -- these just verify the handler layer.
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.storage.movies_store as movies_store
from app.drone_api import Settings
from app.web import handlers_movies


def _build_settings(root: Path, **overrides) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "MOVIES_ROOT": str(root / "movies"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "movies-handler-test",
    }
    env.update(overrides)
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class _FakeHeaders(dict):
    def get(self, key, default=None):
        for existing_key, value in self.items():
            if existing_key.lower() == key.lower():
                return value
        return default


class _FakeHandler:
    def __init__(self, settings: Settings, *, range_header=None) -> None:
        self.settings = settings
        self.headers = _FakeHeaders()
        if range_header is not None:
            self.headers["Range"] = range_header
        self.wfile = io.BytesIO()
        self.response_status = None
        self.response_headers = {}
        self.json_response = None
        self.json_cache_key = "unset"

    # -- JSON path (used by _handle_movies_list) --
    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.json_response = (status_code, payload)
        self.json_cache_key = cache_key

    # -- raw streaming path (used by _stream_movie_range / _stream_file) --
    def send_response(self, status_code: int) -> None:
        self.response_status = status_code

    def send_header(self, key: str, value: str) -> None:
        self.response_headers[key] = value

    def end_headers(self) -> None:
        pass

    def _send_security_headers(self) -> None:
        pass

    def _guess_content_type(self, path: Path) -> str:
        return "video/mp4" if path.suffix == ".mp4" else "application/octet-stream"

    def _stream_file(self, path: Path, content_type: str, as_attachment: bool = False, **kwargs) -> None:
        self.response_status = 200
        self.response_headers["Content-Type"] = content_type
        if as_attachment:
            self.response_headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
        self.wfile.write(path.read_bytes())


def _handler(settings: Settings, **kwargs) -> _FakeHandler:
    class Handler(handlers_movies.HandlersMoviesMixin, _FakeHandler):
        pass

    return Handler(settings, **kwargs)


def _write_movie(root: Path, rel: str, data: bytes) -> Path:
    path = root / "movies" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class MoviesListHandlerTests(unittest.TestCase):
    def test_lists_scanned_movies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Vacation.mp4", b"x" * 100)
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            handler = _handler(settings)
            handler._handle_movies_list()
            status, payload = handler.json_response
            self.assertEqual(status, 200)
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["movies"][0]["movie_name"], "Vacation.mp4")
            self.assertIn("entry_key", payload["movies"][0])

    def test_marks_not_downloadable_when_downloads_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Vacation.mp4", b"x" * 10)
            settings = _build_settings(root, DOWNLOADS_ENABLED="false")
            movies_store.sync_movies_cache(settings.movies_root)
            handler = _handler(settings)
            handler._handle_movies_list()
            _status, payload = handler.json_response
            self.assertFalse(payload["movies"][0]["is_downloadable"])

    def test_query_filters_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Alpha.mp4", b"a")
            _write_movie(root, "Beta.mp4", b"b")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            handler = _handler(settings)
            handler._handle_movies_list(query="beta")
            _status, payload = handler.json_response
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["movies"][0]["movie_name"], "Beta.mp4")

    def test_no_limit_returns_the_whole_inventory_unpaginated(self) -> None:
        # The Movies tab's folder tree needs the complete set client-side to
        # build the on-disk hierarchy -- omitting limit must never truncate.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(5):
                _write_movie(root, f"clips/Clip{index}.mp4", b"x")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            handler = _handler(settings)
            handler._handle_movies_list()
            _status, payload = handler.json_response
            self.assertEqual(payload["count"], 5)
            self.assertEqual(len(payload["movies"]), 5)
            self.assertFalse(payload["has_more"])

    def test_response_is_never_cached(self) -> None:
        # Regression test: ExpiringLRUCache has no invalidation hook, so a
        # cache_key here would let a stale snapshot (e.g. files a scan has
        # since removed) keep being served for up to an hour after the
        # underlying movies cache changed -- this bit us for real (a fixed
        # video-extension filter cleaned the DB, but /movies kept serving a
        # cached response with the old junk files for the rest of that
        # cache entry's TTL). Both the unpaginated and paginated response
        # paths must never pass a cache_key.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Vacation.mp4", b"x")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            handler = _handler(settings)
            handler._handle_movies_list()
            self.assertIsNone(handler.json_cache_key)
            handler2 = _handler(settings)
            handler2._handle_movies_list(limit=10, offset=0)
            self.assertIsNone(handler2.json_cache_key)

    def test_explicit_limit_still_paginates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Alpha.mp4", b"a")
            _write_movie(root, "Beta.mp4", b"b")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            handler = _handler(settings)
            handler._handle_movies_list(limit=1, offset=0)
            _status, payload = handler.json_response
            self.assertEqual(payload["count"], 2)
            self.assertEqual(len(payload["movies"]), 1)
            self.assertTrue(payload["has_more"])


class ResolveMoviePathTests(unittest.TestCase):
    def test_unknown_entry_key_raises_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with self.assertRaises(FileNotFoundError):
                handler._resolve_movie_path("deadbeef")

    def test_malformed_entry_key_raises_not_found_without_a_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(movies_store, "get_movie_by_key") as get_movie:
                with self.assertRaises(FileNotFoundError):
                    handler._resolve_movie_path("../../etc/passwd")
                get_movie.assert_not_called()

    def test_resolves_a_real_movie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_movie(root, "clips/Vacation.mp4", b"data")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]
            handler = _handler(settings)
            resolved = handler._resolve_movie_path(entry_key)
            self.assertEqual(resolved, path.resolve())


class MovieDownloadHandlerTests(unittest.TestCase):
    def test_downloads_disabled_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Vacation.mp4", b"x")
            settings = _build_settings(root, DOWNLOADS_ENABLED="false")
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]
            handler = _handler(settings)
            with self.assertRaises(ValueError):
                handler._handle_movie_download(entry_key)

    def test_streams_full_file_as_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Vacation.mp4", b"movie-bytes")
            settings = _build_settings(root)
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]
            handler = _handler(settings)
            handler._handle_movie_download(entry_key)
            self.assertEqual(handler.response_status, 200)
            self.assertIn("attachment", handler.response_headers["Content-Disposition"])
            self.assertEqual(handler.wfile.getvalue(), b"movie-bytes")


class MovieStreamRangeHandlerTests(unittest.TestCase):
    def _setup(self, tmp, data=b"0123456789"):
        root = Path(tmp)
        _write_movie(root, "Vacation.mp4", data)
        settings = _build_settings(root)
        movies_store.sync_movies_cache(settings.movies_root)
        entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]
        return settings, entry_key

    def test_no_range_header_returns_full_200(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, entry_key = self._setup(tmp, b"0123456789")
            handler = _handler(settings)
            handler._handle_movie_stream(entry_key)
            self.assertEqual(handler.response_status, 200)
            self.assertEqual(handler.response_headers["Accept-Ranges"], "bytes")
            self.assertEqual(handler.response_headers["Content-Length"], "10")
            self.assertNotIn("Content-Range", handler.response_headers)
            self.assertEqual(handler.wfile.getvalue(), b"0123456789")

    def test_bounded_range_returns_206_with_requested_slice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, entry_key = self._setup(tmp, b"0123456789")
            handler = _handler(settings, range_header="bytes=2-4")
            handler._handle_movie_stream(entry_key)
            self.assertEqual(handler.response_status, 206)
            self.assertEqual(handler.response_headers["Content-Range"], "bytes 2-4/10")
            self.assertEqual(handler.response_headers["Content-Length"], "3")
            self.assertEqual(handler.wfile.getvalue(), b"234")

    def test_open_ended_range_returns_rest_of_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, entry_key = self._setup(tmp, b"0123456789")
            handler = _handler(settings, range_header="bytes=7-")
            handler._handle_movie_stream(entry_key)
            self.assertEqual(handler.response_status, 206)
            self.assertEqual(handler.response_headers["Content-Range"], "bytes 7-9/10")
            self.assertEqual(handler.wfile.getvalue(), b"789")

    def test_suffix_range_returns_last_n_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, entry_key = self._setup(tmp, b"0123456789")
            handler = _handler(settings, range_header="bytes=-3")
            handler._handle_movie_stream(entry_key)
            self.assertEqual(handler.response_status, 206)
            self.assertEqual(handler.response_headers["Content-Range"], "bytes 7-9/10")
            self.assertEqual(handler.wfile.getvalue(), b"789")

    def test_malformed_range_falls_back_to_full_200(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, entry_key = self._setup(tmp, b"0123456789")
            handler = _handler(settings, range_header="bytes=banana")
            handler._handle_movie_stream(entry_key)
            self.assertEqual(handler.response_status, 200)
            self.assertEqual(handler.wfile.getvalue(), b"0123456789")

    def test_downloads_disabled_blocks_streaming_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Vacation.mp4", b"x")
            settings = _build_settings(root, DOWNLOADS_ENABLED="false")
            movies_store.sync_movies_cache(settings.movies_root)
            entry_key = movies_store.list_movies(settings.movies_root)[0]["entry_key"]
            handler = _handler(settings)
            with self.assertRaises(ValueError):
                handler._handle_movie_stream(entry_key)


if __name__ == "__main__":
    unittest.main()
