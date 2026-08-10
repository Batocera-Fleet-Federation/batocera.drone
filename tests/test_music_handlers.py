"""HTTP-level tests for HandlersMusicMixin: listing, path-traversal safety,
download, the Range-aware stream endpoint used by the persistent <audio>
player, artwork, batch delete, and the MusicBrainz scraper admin routes.
Mirrors test_movies_handlers.py's shape closely -- see that file and the
drone-movies-feature/drone-music-feature skills for the shared design
rationale. Music scanning/inventory itself is covered in
test_music_store.py, and the scraper's own matching/bulk-job logic in
test_music_metadata_manager.py -- these just verify the handler layer.
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.storage.music_store as music_store
from app.drone_api import Settings
from app.music import metadata_manager as music_metadata
from app.music.musicbrainz_client import MusicBrainzUnavailableError
from app.web import handlers_music


def _build_settings(root: Path, **overrides) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "MOVIES_ROOT": str(root / "movies"),
        "MUSIC_ROOT": str(root / "music"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "music-handler-test",
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

    # -- JSON path (used by _handle_music_list) --
    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.json_response = (status_code, payload)
        self.json_cache_key = cache_key

    # -- raw streaming path (used by _stream_music_range / _stream_file) --
    def send_response(self, status_code: int) -> None:
        self.response_status = status_code

    def send_header(self, key: str, value: str) -> None:
        self.response_headers[key] = value

    def end_headers(self) -> None:
        pass

    def _send_security_headers(self, cache_control: str = "no-store") -> None:
        self.response_headers["Cache-Control"] = cache_control

    def _guess_content_type(self, path: Path) -> str:
        return "audio/mpeg" if path.suffix == ".mp3" else "application/octet-stream"

    def _stream_file(self, path: Path, content_type: str, as_attachment: bool = False, **kwargs) -> None:
        self.response_status = 200
        self.response_headers["Content-Type"] = content_type
        if as_attachment:
            self.response_headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
        self.wfile.write(path.read_bytes())

    def _stream_cached_image(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError()
        self.response_status = 200
        self.response_headers["Content-Type"] = self._guess_content_type(path)
        self.response_headers["Cache-Control"] = "public, max-age=3600"
        self.wfile.write(path.read_bytes())


def _handler(settings: Settings, **kwargs) -> _FakeHandler:
    class Handler(handlers_music.HandlersMusicMixin, _FakeHandler):
        pass

    return Handler(settings, **kwargs)


def _write_track(root: Path, rel: str, data: bytes) -> Path:
    path = root / "music" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class MusicListHandlerTests(unittest.TestCase):
    def test_lists_scanned_music(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x" * 100)
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            handler = _handler(settings)
            handler._handle_music_list()
            status, payload = handler.json_response
            self.assertEqual(status, 200)
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["music"][0]["track_name"], "Track.mp3")
            self.assertIn("entry_key", payload["music"][0])

    def test_marks_not_downloadable_when_downloads_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x" * 10)
            settings = _build_settings(root, DOWNLOADS_ENABLED="false")
            music_store.sync_music_cache(settings.music_root)
            handler = _handler(settings)
            handler._handle_music_list()
            _status, payload = handler.json_response
            self.assertFalse(payload["music"][0]["is_downloadable"])

    def test_query_filters_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Alpha.mp3", b"a")
            _write_track(root, "Artist/Beta.mp3", b"b")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            handler = _handler(settings)
            handler._handle_music_list(query="beta")
            _status, payload = handler.json_response
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["music"][0]["track_name"], "Beta.mp3")

    def test_no_limit_returns_the_whole_inventory_unpaginated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(5):
                _write_track(root, f"Artist/Track{index}.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            handler = _handler(settings)
            handler._handle_music_list()
            _status, payload = handler.json_response
            self.assertEqual(payload["count"], 5)
            self.assertEqual(len(payload["music"]), 5)
            self.assertFalse(payload["has_more"])

    def test_response_is_never_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            handler = _handler(settings)
            handler._handle_music_list()
            self.assertIsNone(handler.json_cache_key)
            handler2 = _handler(settings)
            handler2._handle_music_list(limit=10, offset=0)
            self.assertIsNone(handler2.json_cache_key)

    def test_explicit_limit_still_paginates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Alpha.mp3", b"a")
            _write_track(root, "Artist/Beta.mp3", b"b")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            handler = _handler(settings)
            handler._handle_music_list(limit=1, offset=0)
            _status, payload = handler.json_response
            self.assertEqual(payload["count"], 2)
            self.assertEqual(len(payload["music"]), 1)
            self.assertTrue(payload["has_more"])

    def test_display_title_falls_back_to_the_cleanly_parsed_title_when_unscraped(self) -> None:
        # Not the raw filename -- a track list row that prepends its own
        # "NN - " track-number prefix (the artist/album detail page) would
        # otherwise double up on "01 - 01 - Some Track" for any unscraped
        # track, since the raw filename already carries that same leading
        # number itself. See _apply_music_display_titles's docstring.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/01 - Some Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            handler = _handler(settings)
            handler._handle_music_list()
            _status, payload = handler.json_response
            self.assertEqual(payload["music"][0]["display_title"], "Some Track")

    def test_display_title_falls_back_to_the_whole_stem_when_no_leading_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Some Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            handler = _handler(settings)
            handler._handle_music_list()
            _status, payload = handler.json_response
            self.assertEqual(payload["music"][0]["display_title"], "Some Track")

    def test_display_title_uses_scraped_title_once_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/01 - Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            music_store.save_music_metadata(
                settings.music_root, entry_key, provider="musicbrainz", provider_id="1", title="Real Track Title",
                art_relative_path=None, artist_art_relative_path=None, extra={},
            )
            handler = _handler(settings)
            handler._handle_music_list()
            _status, payload = handler.json_response
            self.assertEqual(payload["music"][0]["display_title"], "Real Track Title")

    def test_artist_and_album_are_parsed_from_folder_structure_without_scraping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Band/Album One/02 - Second Track.mp3", b"x")
            _write_track(root, "Solo Artist/Single.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            handler = _handler(settings)
            handler._handle_music_list()
            _status, payload = handler.json_response
            by_name = {m["track_name"]: m for m in payload["music"]}

            track = by_name["02 - Second Track.mp3"]
            self.assertEqual(track["artist"], "Band")
            self.assertEqual(track["album"], "Album One")
            self.assertEqual(track["track_number"], 2)

            single = by_name["Single.mp3"]
            self.assertEqual(single["artist"], "Solo Artist")
            self.assertEqual(single["album"], "")

    def test_genres_are_empty_until_scraped_then_reflect_saved_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Band/Album/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            handler = _handler(settings)
            handler._handle_music_list()
            _status, payload = handler.json_response
            self.assertEqual(payload["music"][0]["genres"], [])

            entry_key = payload["music"][0]["entry_key"]
            music_store.save_music_metadata(
                settings.music_root, entry_key, provider="musicbrainz", provider_id="1", title="Track",
                art_relative_path=None, artist_art_relative_path=None, extra={"genres": ["Rock"]},
            )
            handler2 = _handler(settings)
            handler2._handle_music_list()
            _status2, payload2 = handler2.json_response
            self.assertEqual(payload2["music"][0]["genres"], ["Rock"])


class MusicLikeHandlerTests(unittest.TestCase):
    def test_list_marks_liked_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            music_store.set_music_liked(settings.music_root, entry_key, True)
            handler = _handler(settings)
            handler._handle_music_list()
            _status, payload = handler.json_response
            self.assertTrue(payload["music"][0]["liked"])

    def test_list_marks_unliked_tracks_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            handler = _handler(settings)
            handler._handle_music_list()
            _status, payload = handler.json_response
            self.assertFalse(payload["music"][0]["liked"])

    def test_detail_reflects_liked_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            music_store.set_music_liked(settings.music_root, entry_key, True)
            handler = _handler(settings)
            handler._handle_music_detail(entry_key)
            _status, payload = handler.json_response
            self.assertTrue(payload["liked"])

    def test_like_toggle_sets_liked_true_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            handler._handle_music_like(entry_key, {})
            status, payload = handler.json_response
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"entry_key": entry_key, "liked": True})
            self.assertTrue(music_store.is_music_liked(settings.music_root, entry_key))

    def test_like_toggle_can_explicitly_unlike(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            music_store.set_music_liked(settings.music_root, entry_key, True)
            handler = _handler(settings)
            handler._handle_music_like(entry_key, {"liked": False})
            status, payload = handler.json_response
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"entry_key": entry_key, "liked": False})
            self.assertFalse(music_store.is_music_liked(settings.music_root, entry_key))

    def test_like_unknown_track_is_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_music_like("deadbeef", {"liked": True})
            status, payload = handler.json_response
            self.assertEqual(status, 404)
            self.assertIn("error", payload)


class ResolveMusicPathTests(unittest.TestCase):
    def test_unknown_entry_key_raises_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with self.assertRaises(FileNotFoundError):
                handler._resolve_music_path("deadbeef")

    def test_malformed_entry_key_raises_not_found_without_a_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(music_store, "get_music_by_key") as get_music:
                with self.assertRaises(FileNotFoundError):
                    handler._resolve_music_path("../../etc/passwd")
                get_music.assert_not_called()

    def test_resolves_a_real_track(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_track(root, "Artist/Track.mp3", b"data")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            resolved = handler._resolve_music_path(entry_key)
            self.assertEqual(resolved, path.resolve())


class MusicDownloadHandlerTests(unittest.TestCase):
    def test_downloads_disabled_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root, DOWNLOADS_ENABLED="false")
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            with self.assertRaises(ValueError):
                handler._handle_music_download(entry_key)

    def test_streams_full_file_as_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"track-bytes")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            handler._handle_music_download(entry_key)
            self.assertEqual(handler.response_status, 200)
            self.assertIn("attachment", handler.response_headers["Content-Disposition"])
            self.assertEqual(handler.wfile.getvalue(), b"track-bytes")


class MusicStreamRangeHandlerTests(unittest.TestCase):
    def _setup(self, tmp, data=b"0123456789"):
        root = Path(tmp)
        _write_track(root, "Artist/Track.mp3", data)
        settings = _build_settings(root)
        music_store.sync_music_cache(settings.music_root)
        entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
        return settings, entry_key

    def test_no_range_header_returns_full_200(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, entry_key = self._setup(tmp, b"0123456789")
            handler = _handler(settings)
            handler._handle_music_stream(entry_key)
            self.assertEqual(handler.response_status, 200)
            self.assertEqual(handler.response_headers["Accept-Ranges"], "bytes")
            self.assertEqual(handler.response_headers["Content-Length"], "10")
            self.assertNotIn("Content-Range", handler.response_headers)
            self.assertEqual(handler.wfile.getvalue(), b"0123456789")
            self.assertEqual(handler.response_headers["Cache-Control"], "public, max-age=300")

    def test_bounded_range_returns_206_with_requested_slice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, entry_key = self._setup(tmp, b"0123456789")
            handler = _handler(settings, range_header="bytes=2-4")
            handler._handle_music_stream(entry_key)
            self.assertEqual(handler.response_status, 206)
            self.assertEqual(handler.response_headers["Content-Range"], "bytes 2-4/10")
            self.assertEqual(handler.response_headers["Content-Length"], "3")
            self.assertEqual(handler.wfile.getvalue(), b"234")

    def test_open_ended_range_returns_rest_of_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, entry_key = self._setup(tmp, b"0123456789")
            handler = _handler(settings, range_header="bytes=7-")
            handler._handle_music_stream(entry_key)
            self.assertEqual(handler.response_status, 206)
            self.assertEqual(handler.response_headers["Content-Range"], "bytes 7-9/10")
            self.assertEqual(handler.wfile.getvalue(), b"789")

    def test_malformed_range_falls_back_to_full_200(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, entry_key = self._setup(tmp, b"0123456789")
            handler = _handler(settings, range_header="bytes=banana")
            handler._handle_music_stream(entry_key)
            self.assertEqual(handler.response_status, 200)
            self.assertEqual(handler.wfile.getvalue(), b"0123456789")

    def test_downloads_disabled_blocks_streaming_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root, DOWNLOADS_ENABLED="false")
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            with self.assertRaises(ValueError):
                handler._handle_music_stream(entry_key)


class MusicDetailHandlerTests(unittest.TestCase):
    def test_unknown_track_raises_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with self.assertRaises(FileNotFoundError):
                handler._handle_music_detail("deadbeef")

    def test_never_scraped_has_null_metadata_and_parsed_filename_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Band/Album/03 - Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            handler._handle_music_detail(entry_key)
            _status, payload = handler.json_response
            self.assertIsNone(payload["metadata"])
            # The cleanly parsed title (leading track number stripped), not
            # the raw filename -- see _apply_music_display_titles's docstring.
            self.assertEqual(payload["display_title"], "Track")
            self.assertEqual(payload["track_number"], 3)
            self.assertEqual(payload["artist"], "Band")
            self.assertEqual(payload["album"], "Album")

    def test_scraped_track_includes_metadata_and_scraped_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Band/Album/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            music_store.save_music_metadata(
                settings.music_root, entry_key, provider="musicbrainz", provider_id="1", title="Real Title",
                art_relative_path=None, artist_art_relative_path=None, extra={"genres": ["Rock"]},
            )
            handler = _handler(settings)
            handler._handle_music_detail(entry_key)
            _status, payload = handler.json_response
            self.assertEqual(payload["display_title"], "Real Title")
            self.assertEqual(payload["metadata"]["genres"], ["Rock"])


class MusicArtworkHandlerTests(unittest.TestCase):
    def _scraped_settings(self, root: Path, *, with_art: bool = True):
        _write_track(root, "Band/Album/Track.mp3", b"x")
        settings = _build_settings(root)
        music_store.sync_music_cache(settings.music_root)
        entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
        art_relative_path = None
        if with_art:
            art_path = root / "music" / "images" / "art.jpg"
            art_path.parent.mkdir(parents=True, exist_ok=True)
            art_path.write_bytes(b"jpeg-bytes")
            art_relative_path = "images/art.jpg"
        music_store.save_music_metadata(
            settings.music_root, entry_key, provider="musicbrainz", provider_id="1", title="Track",
            art_relative_path=art_relative_path, artist_art_relative_path=None, extra={},
        )
        return settings, entry_key

    def test_unknown_field_raises_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, entry_key = self._scraped_settings(Path(tmp))
            handler = _handler(settings)
            with self.assertRaises(FileNotFoundError):
                handler._handle_music_artwork(entry_key, "not-a-real-field")

    def test_never_scraped_raises_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            with self.assertRaises(FileNotFoundError):
                handler._handle_music_artwork(entry_key, "art")

    def test_scraped_but_no_artist_art_raises_not_found_for_that_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, entry_key = self._scraped_settings(Path(tmp))
            handler = _handler(settings)
            with self.assertRaises(FileNotFoundError):
                handler._handle_music_artwork(entry_key, "artist")

    def test_serves_the_art_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings, entry_key = self._scraped_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_music_artwork(entry_key, "art")
            self.assertEqual(handler.response_status, 200)
            self.assertEqual(handler.wfile.getvalue(), b"jpeg-bytes")

    def test_path_traversal_in_stored_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings, entry_key = self._scraped_settings(root, with_art=False)
            music_store.save_music_metadata(
                settings.music_root, entry_key, provider="musicbrainz", provider_id="1", title="Track",
                art_relative_path="../../etc/passwd", artist_art_relative_path=None, extra={},
            )
            handler = _handler(settings)
            with self.assertRaises(FileNotFoundError):
                handler._handle_music_artwork(entry_key, "art")


class MusicArtworkLocalFallbackHandlerTests(unittest.TestCase):
    """A track with no *scraped* art falls back to a conventionally-named
    local image file beside it on disk -- see
    handlers_music._handle_music_artwork's docstring and
    music_store.find_local_cover_image."""

    def test_falls_back_to_local_cover_image_when_never_scraped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Band/Album/Track.mp3", b"x")
            cover_path = root / "music" / "Band" / "Album" / "cover.jpg"
            cover_path.write_bytes(b"local-cover-bytes")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            handler._handle_music_artwork(entry_key, "art")
            self.assertEqual(handler.response_status, 200)
            self.assertEqual(handler.wfile.getvalue(), b"local-cover-bytes")

    def test_scraped_with_no_art_found_still_falls_back_to_local_cover_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Band/Album/Track.mp3", b"x")
            cover_path = root / "music" / "Band" / "Album" / "folder.png"
            cover_path.write_bytes(b"local-folder-bytes")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            # A scrape that matched but Cover Art Archive had no image for
            # the release -- art_relative_path stays None even though a
            # metadata row exists.
            music_store.save_music_metadata(
                settings.music_root, entry_key, provider="musicbrainz", provider_id="1", title="Track",
                art_relative_path=None, artist_art_relative_path=None, extra={},
            )
            handler = _handler(settings)
            handler._handle_music_artwork(entry_key, "art")
            self.assertEqual(handler.response_status, 200)
            self.assertEqual(handler.wfile.getvalue(), b"local-folder-bytes")

    def test_scraped_art_takes_priority_over_a_local_cover_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings, entry_key = self._scraped_settings_with_art(root)
            cover_path = root / "music" / "Band" / "Album" / "cover.jpg"
            cover_path.write_bytes(b"local-cover-bytes")
            handler = _handler(settings)
            handler._handle_music_artwork(entry_key, "art")
            self.assertEqual(handler.wfile.getvalue(), b"scraped-art-bytes")

    def test_local_fallback_does_not_apply_to_the_artist_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Band/Album/Track.mp3", b"x")
            cover_path = root / "music" / "Band" / "Album" / "cover.jpg"
            cover_path.write_bytes(b"local-cover-bytes")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            with self.assertRaises(FileNotFoundError):
                handler._handle_music_artwork(entry_key, "artist")

    def test_neither_scraped_nor_local_art_raises_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Band/Album/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            with self.assertRaises(FileNotFoundError):
                handler._handle_music_artwork(entry_key, "art")

    def _scraped_settings_with_art(self, root: Path):
        _write_track(root, "Band/Album/Track.mp3", b"x")
        settings = _build_settings(root)
        music_store.sync_music_cache(settings.music_root)
        entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
        art_path = root / "music" / "images" / "art.jpg"
        art_path.parent.mkdir(parents=True, exist_ok=True)
        art_path.write_bytes(b"scraped-art-bytes")
        music_store.save_music_metadata(
            settings.music_root, entry_key, provider="musicbrainz", provider_id="1", title="Track",
            art_relative_path="images/art.jpg", artist_art_relative_path=None, extra={},
        )
        return settings, entry_key


class MusicDeleteHandlerTests(unittest.TestCase):
    def test_deletes_a_single_track(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)

            handler._handle_admin_music_delete({"entry_keys": [entry_key]})

            status, payload = handler.json_response
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"deleted": 1, "requested": 1})
            self.assertFalse(path.exists())

    def test_deletes_a_batch_and_counts_only_the_ones_that_existed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track1.mp3", b"x")
            _write_track(root, "Artist/Track2.mp3", b"y")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_keys = [row["entry_key"] for row in music_store.list_music(settings.music_root)]
            handler = _handler(settings)

            handler._handle_admin_music_delete({"entry_keys": entry_keys + ["not-a-real-key"]})

            status, payload = handler.json_response
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"deleted": 2, "requested": 3})
            self.assertEqual(music_store.list_music(settings.music_root), [])

    def test_delete_also_removes_scraped_metadata_and_artwork(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            art_path = root / "music" / "images" / "art.jpg"
            art_path.parent.mkdir(parents=True, exist_ok=True)
            art_path.write_bytes(b"jpeg-bytes")
            music_store.save_music_metadata(
                settings.music_root, entry_key, provider="musicbrainz", provider_id="1", title="Track",
                art_relative_path="images/art.jpg", artist_art_relative_path=None, extra={},
            )
            handler = _handler(settings)

            handler._handle_admin_music_delete({"entry_keys": [entry_key]})

            self.assertIsNone(music_store.get_music_metadata(settings.music_root, entry_key))
            self.assertFalse(art_path.exists())

    def test_missing_entry_keys_is_a_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_music_delete({})
            status, payload = handler.json_response
            self.assertEqual(status, 400)
            self.assertIn("entry_keys", payload["error"])

    def test_empty_entry_keys_list_is_a_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_music_delete({"entry_keys": []})
            status, _ = handler.json_response
            self.assertEqual(status, 400)


class MusicScrapeSearchHandlerTests(unittest.TestCase):
    def test_unknown_track_is_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_music_scrape_search("deadbeef", "some query")
            status, _payload = handler.json_response
            self.assertEqual(status, 404)

    def test_defaults_query_to_the_search_ladder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            with mock.patch.object(
                music_metadata, "search_track_default_query",
                return_value={"query": "Sample Artist Sample Album", "results": [{"release_mbid": "release-1", "kind": "release"}]},
            ) as ladder_search:
                handler._handle_admin_music_scrape_search(entry_key, None)
            ladder_search.assert_called_once_with(settings, "Sample Artist", "Sample Album", "Track One")
            status, payload = handler.json_response
            self.assertEqual(status, 200)
            self.assertEqual(payload["query"], "Sample Artist Sample Album")
            self.assertEqual(payload["results"], [{"release_mbid": "release-1", "kind": "release"}])

    def test_explicit_query_overrides_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "search", return_value=[{"release_mbid": "r1"}]) as search:
                handler._handle_admin_music_scrape_search(entry_key, "custom query")
            search.assert_called_once_with(settings, "custom query")
            _status, payload = handler.json_response
            self.assertEqual(payload["results"], [{"release_mbid": "r1"}])

    def test_musicbrainz_unavailable_is_502(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "search", side_effect=MusicBrainzUnavailableError("rate limited")):
                handler._handle_admin_music_scrape_search(entry_key, "matrix")
            status, payload = handler.json_response
            self.assertEqual(status, 502)
            self.assertIn("rate limited", payload["error"])


class MusicScrapeApplyHandlerTests(unittest.TestCase):
    def test_missing_release_mbid_is_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_music_scrape_apply("deadbeef", {})
            status, payload = handler.json_response
            self.assertEqual(status, 400)
            self.assertIn("release_mbid", payload["error"])

    def test_unknown_track_is_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_music_scrape_apply("deadbeef", {"release_mbid": "release-1"})
            status, _payload = handler.json_response
            self.assertEqual(status, 404)

    def test_match_error_is_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "apply", side_effect=music_metadata.MusicMatchError("no match")):
                handler._handle_admin_music_scrape_apply(entry_key, {"release_mbid": "release-1"})
            status, payload = handler.json_response
            self.assertEqual(status, 400)
            self.assertIn("no match", payload["error"])

    def test_musicbrainz_unavailable_is_502(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "apply", side_effect=MusicBrainzUnavailableError("down")):
                handler._handle_admin_music_scrape_apply(entry_key, {"release_mbid": "release-1"})
            status, payload = handler.json_response
            self.assertEqual(status, 502)
            self.assertIn("down", payload["error"])

    def test_successful_apply_passes_through_recording_mbid_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "apply", return_value={"title": "Matched Track"}) as apply_fn:
                handler._handle_admin_music_scrape_apply(entry_key, {"release_mbid": "release-1", "recording_mbid": "rec-1"})
            apply_fn.assert_called_once_with(settings, entry_key, "release-1", recording_mbid="rec-1")
            status, payload = handler.json_response
            self.assertEqual(status, 200)
            self.assertEqual(payload["title"], "Matched Track")


class MusicScrapeDeleteHandlerTests(unittest.TestCase):
    def test_deletes_and_returns_the_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Artist/Track.mp3", b"x")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            music_store.save_music_metadata(
                settings.music_root, entry_key, provider="musicbrainz", provider_id="1", title="Track",
                art_relative_path=None, artist_art_relative_path=None, extra={},
            )
            handler = _handler(settings)
            handler._handle_admin_music_scrape_delete(entry_key)
            status, payload = handler.json_response
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"deleted": True})
            self.assertIsNone(music_store.get_music_metadata(settings.music_root, entry_key))

    def test_never_scraped_still_returns_200_with_deleted_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_music_scrape_delete("deadbeef")
            status, payload = handler.json_response
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"deleted": False})


class MusicBulkScrapeHandlerTests(unittest.TestCase):
    def test_status_wraps_get_bulk_scrape_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "get_bulk_scrape_status", return_value={"status": "complete"}) as status_fn:
                handler._handle_admin_music_scrape_bulk_status()
            status_fn.assert_called_once_with(settings)
            status, payload = handler.json_response
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"job": {"status": "complete"}})

    def test_status_reports_null_job_when_none_has_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_music_scrape_bulk_status()
            _status, payload = handler.json_response
            self.assertIsNone(payload["job"])

    def test_start_defaults_rescan_all_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "start_bulk_scrape", return_value={"status": "ok", "job": {}}) as start_fn:
                handler._handle_admin_music_scrape_bulk_start({})
            start_fn.assert_called_once_with(settings, rescan_all=False)

    def test_start_passes_through_rescan_all_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "start_bulk_scrape", return_value={"status": "ok", "job": {}}) as start_fn:
                handler._handle_admin_music_scrape_bulk_start({"rescan_all": True})
            start_fn.assert_called_once_with(settings, rescan_all=True)

    def test_already_running_is_409(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "start_bulk_scrape", return_value={"status": "already_running"}):
                handler._handle_admin_music_scrape_bulk_start({})
            status, _payload = handler.json_response
            self.assertEqual(status, 409)

    def test_ok_start_returns_the_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "start_bulk_scrape", return_value={"status": "ok", "job": {"id": 1}}):
                handler._handle_admin_music_scrape_bulk_start({})
            status, payload = handler.json_response
            self.assertEqual(status, 200)
            self.assertEqual(payload["job"], {"id": 1})

    def test_non_dict_payload_is_treated_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "start_bulk_scrape", return_value={"status": "ok", "job": {}}) as start_fn:
                handler._handle_admin_music_scrape_bulk_start(None)
            start_fn.assert_called_once_with(settings, rescan_all=False)


class MusicBulkScrapeItemsHandlerTests(unittest.TestCase):
    def test_lists_items_for_a_valid_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "get_bulk_scrape_items", return_value={"total": 0, "items": []}) as items_fn:
                handler._handle_admin_music_scrape_bulk_items("failed", None, 0)
            items_fn.assert_called_once_with(settings, "failed", limit=200, offset=0)

    def test_invalid_status_is_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_music_scrape_bulk_items("bogus", None, 0)
            status, _payload = handler.json_response
            self.assertEqual(status, 400)

    def test_passes_through_limit_and_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "get_bulk_scrape_items", return_value={"total": 0, "items": []}) as items_fn:
                handler._handle_admin_music_scrape_bulk_items("matched", 50, 10)
            items_fn.assert_called_once_with(settings, "matched", limit=50, offset=10)


class MusicBulkScrapeRetryHandlerTests(unittest.TestCase):
    def test_retry_by_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "retry_bulk_scrape_items", return_value={"status": "ok", "job": {}}) as retry_fn:
                handler._handle_admin_music_scrape_bulk_retry({"status": "failed"})
            retry_fn.assert_called_once_with(settings, status="failed", entry_keys=None)

    def test_retry_by_entry_keys_ignores_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "retry_bulk_scrape_items", return_value={"status": "ok", "job": {}}) as retry_fn:
                handler._handle_admin_music_scrape_bulk_retry({"entry_keys": ["a", "b"], "status": "failed"})
            retry_fn.assert_called_once_with(settings, status=None, entry_keys=["a", "b"])

    def test_missing_both_entry_keys_and_status_is_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_music_scrape_bulk_retry({})
            status, _payload = handler.json_response
            self.assertEqual(status, 400)

    def test_already_running_is_409(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(music_metadata, "retry_bulk_scrape_items", return_value={"status": "already_running"}):
                handler._handle_admin_music_scrape_bulk_retry({"status": "failed"})
            status, _payload = handler.json_response
            self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()
