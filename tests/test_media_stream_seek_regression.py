"""Regression for Issue #677 seeking + remote play.

Verifies:
- remote media keeps Play action (no is_local gate)
- no 'Play from {drone}' label remains
- movie/music Range handling returns 206 for valid range and 416 for unsatisfiable
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.storage import movies_store, music_store
from app.web import handlers_movies, handlers_music


def _build_settings(root: Path, **overrides) -> Settings:
    env = {
        "USERDATA_ROOT": str(root / "userdata"),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.db"),
        "DRONE_DEVICE_ID": "test-drone",
    }
    env.update(overrides)
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class _FakeHeaders(dict):
    def get(self, key, default=None):
        # case-insensitive like HTTPMessage
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _FakeHandlerMovies(handlers_movies.HandlersMoviesMixin):
    def __init__(self, settings, range_header=None):
        self.settings = settings
        self.headers = _FakeHeaders()
        if range_header is not None:
            self.headers["Range"] = range_header
        self.wfile = io.BytesIO()
        self.response_status = None
        self.response_headers = {}

    def send_response(self, code):
        self.response_status = code

    def send_header(self, k, v):
        self.response_headers[k] = v

    def end_headers(self):
        pass

    def _send_security_headers(self, cache_control="no-store"):
        self.response_headers["Cache-Control"] = cache_control

    def _guess_content_type(self, path: Path):
        return "video/mp4"


class _FakeHandlerMusic(handlers_music.HandlersMusicMixin):
    def __init__(self, settings, range_header=None):
        self.settings = settings
        self.headers = _FakeHeaders()
        if range_header is not None:
            self.headers["Range"] = range_header
        self.wfile = io.BytesIO()
        self.response_status = None
        self.response_headers = {}

    def send_response(self, code):
        self.response_status = code

    def send_header(self, k, v):
        self.response_headers[k] = v

    def end_headers(self):
        pass

    def _send_security_headers(self, cache_control="no-store"):
        self.response_headers["Cache-Control"] = cache_control

    def _guess_content_type(self, path: Path):
        return "audio/mpeg"


class MediaStreamSeekRegressionTests(unittest.TestCase):
    def test_movie_seek_returns_206(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movie_file = root / "movie.mp4"
            data = b"0123456789" * 100  # 1000 bytes
            movie_file.write_bytes(data)
            settings = _build_settings(root)
            handler = _FakeHandlerMovies(settings, range_header="bytes=100-199")
            handler._stream_movie_range(movie_file, "video/mp4")
            self.assertEqual(handler.response_status, 206)
            self.assertEqual(handler.response_headers["Content-Range"], "bytes 100-199/1000")
            self.assertEqual(int(handler.response_headers["Content-Length"]), 100)
            self.assertEqual(handler.wfile.getvalue(), data[100:200])

    def test_movie_unsatisfiable_range_returns_416(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movie_file = root / "movie.mp4"
            movie_file.write_bytes(b"0123456789")
            settings = _build_settings(root)
            handler = _FakeHandlerMovies(settings, range_header="bytes=9999-10000")
            handler._stream_movie_range(movie_file, "video/mp4")
            self.assertEqual(handler.response_status, 416)
            self.assertEqual(handler.response_headers["Content-Range"], "bytes */10")

    def test_music_seek_returns_206(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music_file = root / "song.mp3"
            music_file.write_bytes(b"ABCDEFGHIJ" * 100)
            settings = _build_settings(root)
            handler = _FakeHandlerMusic(settings, range_header="bytes=10-19")
            handler._stream_music_range(music_file, "audio/mpeg")
            self.assertEqual(handler.response_status, 206)
            self.assertEqual(handler.response_headers["Content-Range"], "bytes 10-19/1000")

    def test_media_browse_play_button_allows_remote(self):
        script = (Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "media_browse.js").read_text(encoding="utf-8")
        self.assertIn('const play = kind !== "roms" && it.entry_key', script)
        self.assertNotIn('const play = kind !== "roms" && it.entry_key && it.is_local', script)
        self.assertNotIn('" from " + esc(droneLabel(it))', script)
        # also ensure no "Playing from" label remains
        self.assertNotIn("Playing from", script)


if __name__ == "__main__":
    unittest.main()
