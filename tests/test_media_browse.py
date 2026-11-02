"""Multi-Drone media browsing: merge/paging logic, the peer media surface
(music inventory, Range-aware download, artwork/detail), the playback proxy,
local-only likes, and copying a track/game via the normal download path."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import app.storage.music_store as music_store
from app.drone_api import Settings
from app.transfer import media_browse
from app.transfer.media_browse import MediaSource
from app.transfer.peer_connectivity import PeerIdentityHold
from app.web import handlers_media_browse
from app.web.handlers_media_browse import HandlersMediaBrowseMixin
from app.web.handlers_movies import HandlersMoviesMixin
from app.web.handlers_music import HandlersMusicMixin
from app.web.handlers_peer import HandlersPeerMixin


def _settings(root: Path, device_id: str = "drone-local") -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "MOVIES_ROOT": str(root / "movies"),
        "SHOWS_ROOT": str(root / "shows"),
        "MUSIC_ROOT": str(root / "music"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": device_id,
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class _Headers(dict):
    def get(self, key, default=None):
        for existing, value in self.items():
            if existing.lower() == key.lower():
                return value
        return default


class _Base:
    def __init__(self, settings, range_header=None):
        self.settings = settings
        self.headers = _Headers()
        if range_header:
            self.headers["Range"] = range_header
        self.wfile = io.BytesIO()
        self.status = None
        self.out_headers = {}
        self.json = None

    def _send_json(self, status, payload, cache_key=None, extra_headers=None):
        self.json = (status, payload)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.out_headers[key] = value

    def end_headers(self):
        pass

    def _send_security_headers(self, cache_control="no-store", csp_override=None):
        pass

    def _guess_content_type(self, path):
        return "audio/mpeg" if Path(path).suffix == ".mp3" else "video/mp4"

    def _peer_request_authorized(self):
        return True

    def _peer_requester_device_id(self):
        return "peer-x"

    def log_message(self, *args, **kwargs):
        pass

    def log_error(self, *args, **kwargs):
        pass

    def _stream_file(self, path, content_type, as_attachment=False, **kwargs):
        self.status = 200
        self.wfile.write(Path(path).read_bytes())


class _Handler(HandlersMediaBrowseMixin, HandlersPeerMixin, HandlersMoviesMixin, HandlersMusicMixin, _Base):
    pass


class _FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self._body = io.BytesIO(body)
        self.status = status
        self.headers = _Headers(headers or {})

    def read(self, n=-1):
        return self._body.read(n)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _src(drone_id, name=None, local=False):
    return MediaSource(drone_id=drone_id, name=name or drone_id, is_local=local)


def _paged(items_by_source):
    def fetch(source, offset, limit):
        items = items_by_source[source.drone_id]
        return {"total": len(items), "items": items[offset:offset + limit]}
    return fetch


class MergeAndPagingTests(unittest.TestCase):
    def setUp(self):
        self.sources = [_src("b"), _src("local", local=True), _src("a")]
        self.data = {
            "local": [{"entry_key": f"l{i}"} for i in range(5)],
            "a": [{"entry_key": f"a{i}"} for i in range(3)],
            "b": [{"entry_key": f"b{i}"} for i in range(4)],
        }

    def _walk(self, limit):
        seen, cursor, pages = [], "", 0
        while True:
            page = media_browse.collect_page(self.sources, _paged(self.data), limit=limit, cursor=cursor)
            seen.extend((i["drone_id"], i["entry_key"]) for i in page["items"])
            pages += 1
            if not page["has_more"]:
                return seen, page, pages
            cursor = page["next_cursor"]

    def test_every_item_exactly_once_and_deterministic(self):
        first, _, _ = self._walk(4)
        second, _, _ = self._walk(4)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 12)
        self.assertEqual(len(set(first)), 12)

    def test_local_first_then_peers_by_id_and_tagged(self):
        page = media_browse.collect_page(self.sources, _paged(self.data), limit=6)
        self.assertEqual([i["drone_id"] for i in page["items"]], ["local", "a", "b", "local", "a", "b"])
        self.assertTrue(page["items"][0]["is_local"])
        self.assertFalse(page["items"][1]["is_local"])
        self.assertEqual(page["total"], 12)

    def test_same_cursor_same_page(self):
        page = media_browse.collect_page(self.sources, _paged(self.data), limit=5)
        again = media_browse.collect_page(self.sources, _paged(self.data), limit=5)
        self.assertEqual(page, again)
        p2 = media_browse.collect_page(self.sources, _paged(self.data), limit=5, cursor=page["next_cursor"])
        p2b = media_browse.collect_page(self.sources, _paged(self.data), limit=5, cursor=page["next_cursor"])
        self.assertEqual(p2, p2b)

    def test_bad_cursor_starts_over(self):
        page = media_browse.collect_page(self.sources, _paged(self.data), limit=3, cursor="!!notbase64")
        self.assertEqual(len(page["items"]), 3)

    def test_offline_timeout_unsupported_do_not_fail_the_page(self):
        def fetch(source, offset, limit):
            if source.drone_id == "a":
                raise OSError("connection refused")
            if source.drone_id == "b":
                raise media_browse.SourceUnsupported()
            if source.drone_id == "c":
                raise media_browse.SourceTimeout()
            return {"total": 2, "items": [{"entry_key": "x"}, {"entry_key": "y"}]}

        page = media_browse.collect_page([_src("local", local=True), _src("a"), _src("b"), _src("c")], fetch, limit=10)
        status = {s["drone_id"]: s["status"] for s in page["sources"]}
        self.assertEqual(status, {"local": "ok", "a": "offline", "b": "unsupported", "c": "timeout"})
        self.assertEqual(len(page["items"]), 2)
        self.assertFalse(page["has_more"])

    def test_offline_source_keeps_its_cursor_offset(self):
        cursor = media_browse.encode_cursor({"a": 2})
        page = media_browse.collect_page([_src("a")], lambda *args: (_ for _ in ()).throw(OSError("down")), limit=5, cursor=cursor)
        self.assertEqual(media_browse.decode_cursor(page["next_cursor"] or cursor), {"a": 2})

    def test_matching(self):
        a = {"display_title": "The Matrix (1999)", "fingerprint": "abc"}
        self.assertTrue(media_browse.same_item("movies", a, {"fingerprint": "ABC", "display_title": "other"}))
        self.assertTrue(media_browse.same_item("movies", a, {"display_title": "the matrix 1999"}))
        self.assertFalse(media_browse.same_item("movies", {"display_title": "Heat", "year": "1995"}, {"display_title": "Heat", "year": "2013"}))
        self.assertFalse(media_browse.same_item("music", {"display_title": "Intro", "artist": "A"}, {"display_title": "Intro", "artist": "B"}))
        self.assertFalse(media_browse.same_item("roms", {"display_title": "Sonic", "system": "genesis"}, {"display_title": "Sonic", "system": "snes"}))
        self.assertTrue(media_browse.same_item("roms", {"display_title": "Sonic", "system": "genesis"}, {"display_title": "Sonic", "system": "genesis"}))


class PeerSurfaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = _settings(self.root)
        track = self.root / "music" / "Artist" / "Album" / "01 - Song.mp3"
        track.parent.mkdir(parents=True)
        track.write_bytes(b"0123456789" * 100)
        music_store.sync_music_cache(self.settings.music_root)
        self.entry_key = music_store.list_music(self.settings.music_root)[0]["entry_key"]
        music_store.set_music_liked(self.settings.music_root, self.entry_key, True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_music_inventory_has_titles_no_paths_no_likes(self):
        handler = _Handler(self.settings)
        result = handler._collect_peer_inventory("music", {"limit": ["50"]})
        self.assertEqual(result["asset_type"], "music")
        self.assertEqual(result["total"], 1)
        item = result["items"][0]
        self.assertNotIn("absolute_path", item)
        self.assertNotIn("liked", item)
        self.assertEqual(item["artist"], "Artist")
        self.assertTrue(item["display_title"])

    def test_summary_reports_media_counts(self):
        handler = _Handler(self.settings)
        counts = handler._local_media_counts()
        self.assertEqual(counts["music"], 1)
        self.assertEqual(counts["movies"], 0)

    def test_peer_detail_strips_likes_but_local_detail_keeps_them(self):
        handler = _Handler(self.settings)
        handler._handle_peer_media_detail("music", self.entry_key)
        status, payload = handler.json
        self.assertEqual(status, 200)
        self.assertNotIn("liked", payload)
        self.assertNotIn("absolute_path", payload)
        self.assertTrue(handler._local_media_detail("music", self.entry_key)["liked"])

    def test_peer_detail_rejects_bad_keys(self):
        handler = _Handler(self.settings)
        handler._handle_peer_media_detail("music", "../../etc")
        self.assertEqual(handler.json[0], 404)

    def test_peer_music_download_rejects_traversal(self):
        for bad in ("../secret", "Artist/../../x", ""):
            handler = _Handler(self.settings)
            handler._handle_peer_music_download(bad)
            self.assertEqual(handler.json[0], 400, bad)

    def test_peer_music_download_missing_is_404(self):
        handler = _Handler(self.settings)
        handler._handle_peer_music_download("Artist/nope.mp3")
        self.assertEqual(handler.json[0], 404)

    def test_peer_music_range_206_and_416(self):
        handler = _Handler(self.settings, range_header="bytes=10-19")
        handler._handle_peer_music_download("Artist/Album/01 - Song.mp3")
        self.assertEqual(handler.status, 206)
        self.assertEqual(handler.out_headers["Content-Range"], "bytes 10-19/1000")
        self.assertEqual(handler.wfile.getvalue(), b"0123456789")
        past_end = _Handler(self.settings, range_header="bytes=5000-")
        past_end._handle_peer_music_download("Artist/Album/01 - Song.mp3")
        self.assertEqual(past_end.status, 416)

    def test_peer_artwork_unknown_type_404(self):
        handler = _Handler(self.settings)
        handler._handle_peer_media_artwork("bios", "ab", "poster")
        self.assertEqual(handler.json[0], 404)


class PlaybackProxyAndLikesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = _settings(self.root)
        self.peer = {"drone_id": "drone-peer", "name": "Peer"}
        patcher = mock.patch.object(HandlersMediaBrowseMixin, "_media_peer", lambda self, drone_id: {"drone_id": "drone-peer", "name": "Peer"} if drone_id == "drone-peer" else None)
        patcher.start()
        self.addCleanup(patcher.stop)
        handlers_media_browse._path_cache.clear()

    def tearDown(self):
        self._tmp.cleanup()

    def _detail(self):
        return mock.patch.object(
            handlers_media_browse, "_peer_get_json_for_peer",
            return_value=({"file_path": "Shows/Show/S01E01.mkv", "entry_key": "ab"}, "http://peer"),
        )

    def test_stream_proxy_forwards_range_and_passes_206_through(self):
        response = _FakeResponse(b"abc", status=206, headers={"Content-Type": "video/x-matroska", "Content-Length": "3", "Content-Range": "bytes 0-2/10"})
        with self._detail(), mock.patch.object(handlers_media_browse, "_peer_open_stream_for_peer", return_value=(response, "http://peer")) as opener:
            handler = _Handler(self.settings, range_header="bytes=0-2")
            handler._handle_media_browse_stream("movies", "drone-peer", "ab")
        self.assertEqual(handler.status, 206)
        self.assertEqual(handler.out_headers["Content-Range"], "bytes 0-2/10")
        self.assertEqual(handler.wfile.getvalue(), b"abc")
        self.assertEqual(opener.call_args.kwargs["range_header"], "bytes=0-2")
        self.assertEqual(opener.call_args.args[1], "/v1/api/peer/movies/Shows/Show/S01E01.mkv")

    def test_stream_proxy_416_is_passed_through(self):
        error = HTTPError("http://peer", 416, "bad range", _Headers({"Content-Range": "bytes */10"}), None)
        with self._detail(), mock.patch.object(handlers_media_browse, "_peer_open_stream_for_peer", side_effect=error):
            handler = _Handler(self.settings, range_header="bytes=99-")
            handler._handle_media_browse_stream("movies", "drone-peer", "ab")
        self.assertEqual(handler.status, 416)

    def test_stream_proxy_unreachable_peer_is_502_and_hold_is_503(self):
        with self._detail(), mock.patch.object(handlers_media_browse, "_peer_open_stream_for_peer", side_effect=OSError("down")):
            handler = _Handler(self.settings)
            handler._handle_media_browse_stream("music", "drone-peer", "ab")
        self.assertEqual(handler.json[0], 502)
        handlers_media_browse._path_cache.clear()
        with self._detail(), mock.patch.object(handlers_media_browse, "_peer_open_stream_for_peer", side_effect=PeerIdentityHold("hold")):
            handler = _Handler(self.settings)
            handler._handle_media_browse_stream("music", "drone-peer", "ab")
        self.assertEqual(handler.json[0], 503)

    def test_stream_proxy_rejects_unknown_drone_and_bad_key(self):
        handler = _Handler(self.settings)
        handler._handle_media_browse_stream("movies", "stranger", "ab")
        self.assertEqual(handler.json[0], 404)
        handler = _Handler(self.settings)
        handler._handle_media_browse_stream("movies", "drone-peer", "../../x")
        self.assertEqual(handler.json[0], 404)

    def test_peer_path_traversal_in_detail_is_refused(self):
        with mock.patch.object(handlers_media_browse, "_peer_get_json_for_peer", return_value=({"file_path": "../../etc/passwd"}, "x")):
            handler = _Handler(self.settings)
            handler._handle_media_browse_stream("movies", "drone-peer", "ab")
        self.assertEqual(handler.json[0], 404)

    def test_artwork_proxy_requires_image_content_type(self):
        bad = _FakeResponse(b"<html>", headers={"Content-Type": "text/html"})
        with mock.patch.object(handlers_media_browse, "_peer_open_stream_for_peer", return_value=(bad, "x")):
            handler = _Handler(self.settings)
            handler._handle_media_browse_artwork("movies", "drone-peer", "ab", "poster")
        self.assertEqual(handler.json[0], 502)
        good = _FakeResponse(b"\xff\xd8img", headers={"Content-Type": "image/jpeg"})
        with mock.patch.object(handlers_media_browse, "_peer_open_stream_for_peer", return_value=(good, "x")) as opener:
            handler = _Handler(self.settings)
            handler._handle_media_browse_artwork("movies", "drone-peer", "ab", "poster")
        self.assertEqual(handler.status, 200)
        self.assertEqual(opener.call_args.args[1], "/v1/api/peer/media-artwork/movies/ab/poster")

    def test_rom_artwork_proxy_rejects_traversal(self):
        handler = _Handler(self.settings)
        handler._handle_media_browse_rom_artwork("drone-peer", "snes", "image", "../../x.png")
        self.assertEqual(handler.json[0], 400)

    def test_peer_likes_stay_local_and_never_call_the_peer(self):
        with mock.patch.object(handlers_media_browse, "_peer_get_json_for_peer") as get, \
                mock.patch.object(handlers_media_browse, "_peer_open_stream_for_peer") as opener:
            handler = _Handler(self.settings)
            handler._handle_media_browse_like({"drone_id": "drone-peer", "entry_key": "ab", "liked": True})
            self.assertEqual(handler.json[0], 200)
            get.assert_not_called()
            opener.assert_not_called()
        self.assertEqual(music_store.list_peer_music_liked_keys(self.settings.music_root, "drone-peer"), {"ab"})
        items = [{"entry_key": "ab", "drone_id": "drone-peer", "is_local": False}, {"entry_key": "ab", "drone_id": "drone-local", "is_local": True}]
        _Handler(self.settings)._media_overlay_likes(items)
        self.assertEqual([i["liked"] for i in items], [True, False])
        handler = _Handler(self.settings)
        handler._handle_media_browse_like({"drone_id": "drone-peer", "entry_key": "ab", "liked": False})
        self.assertEqual(music_store.list_peer_music_liked_keys(self.settings.music_root, "drone-peer"), set())

    def test_like_for_unknown_drone_or_key_is_rejected(self):
        handler = _Handler(self.settings)
        handler._handle_media_browse_like({"drone_id": "stranger", "entry_key": "ab"})
        self.assertEqual(handler.json[0], 404)
        handler = _Handler(self.settings)
        handler._handle_media_browse_like({"drone_id": "drone-peer", "entry_key": "zz!"})
        self.assertEqual(handler.json[0], 400)

    def test_swarm_reset_clears_peer_likes(self):
        music_store.set_peer_music_liked(self.settings.music_root, "drone-peer", "ab", True)
        from app.transfer import swarm_reset
        swarm_reset._reset_peer_music_likes(self.settings)
        self.assertEqual(music_store.list_peer_music_liked_keys(self.settings.music_root, "drone-peer"), set())


class MergedListHandlerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = _settings(self.root)
        track = self.root / "music" / "Local Artist" / "Song.mp3"
        track.parent.mkdir(parents=True)
        track.write_bytes(b"x" * 64)
        music_store.sync_music_cache(self.settings.music_root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_list_merges_local_and_peer_and_survives_offline_peer(self):
        peers = [{"drone_id": "drone-ok", "name": "Ok"}, {"drone_id": "drone-down", "name": "Down"}, {"drone_id": "drone-old", "name": "Old"}]

        def peer_request(self, peer, peer_id, kind, query_params, overrides=None):
            if peer_id == "drone-down":
                raise OSError("unreachable")
            if peer_id == "drone-old":
                raise HTTPError("u", 400, "asset type must be", {}, None)
            return {"total": 1, "items": [{"entry_key": "ff", "display_title": "Peer Song", "fingerprint": "f"}]}

        with mock.patch("app.transfer.local_network.is_local_mode", return_value=True), \
                mock.patch("app.transfer.local_network.paired_peers", return_value=peers), \
                mock.patch("app.transfer.local_network.get_paired_peer", side_effect=lambda settings, pid: next((p for p in peers if p["drone_id"] == pid), None)), \
                mock.patch.object(HandlersMediaBrowseMixin, "_peer_inventory_request", peer_request, create=True):
            handler = _Handler(self.settings)
            handler._handle_media_browse_list("music", {"limit": ["20"]})
        status, body = handler.json
        self.assertEqual(status, 200)
        by_drone = {s["drone_id"]: s["status"] for s in body["sources"]}
        self.assertEqual(by_drone["drone-local"], "ok")
        self.assertEqual(by_drone["drone-ok"], "ok")
        self.assertEqual(by_drone["drone-down"], "offline")
        self.assertEqual(by_drone["drone-old"], "unsupported")
        self.assertEqual({i["drone_id"] for i in body["items"]}, {"drone-local", "drone-ok"})
        self.assertTrue(all("drone_name" in i for i in body["items"]))
        self.assertFalse(any("absolute_path" in i for i in body["items"]))

    def test_unknown_media_type_is_404(self):
        handler = _Handler(self.settings)
        handler._handle_media_browse_list("bios", {})
        self.assertEqual(handler.json[0], 404)


class CachingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = _settings(self.root)
        (self.root / "music").mkdir()
        music_store.sync_music_cache(self.settings.music_root)
        handlers_media_browse._source_cache.invalidate()
        self.calls = 0
        self.fail = False
        peers = [{"drone_id": "drone-ok", "name": "Ok"}]

        def peer_request(inner_self, peer, peer_id, kind, query_params, overrides=None):
            self.calls += 1
            if self.fail:
                raise OSError("down")
            return {"total": 1, "items": [{"entry_key": "ff", "display_title": "Peer Song"}]}

        patches = [
            mock.patch("app.transfer.local_network.is_local_mode", return_value=True),
            mock.patch("app.transfer.local_network.paired_peers", return_value=peers),
            mock.patch("app.transfer.local_network.get_paired_peer", side_effect=lambda settings, pid: peers[0] if pid == "drone-ok" else None),
            mock.patch.object(HandlersMediaBrowseMixin, "_peer_inventory_request", peer_request, create=True),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        handlers_media_browse._source_cache.invalidate()
        self._tmp.cleanup()

    def _list(self, **extra):
        handler = _Handler(self.settings)
        handler._handle_media_browse_list("music", {"limit": ["20"], **extra})
        return handler.json[1]

    def test_second_load_does_not_ask_the_peer_again(self):
        self._list()
        self._list()
        self.assertEqual(self.calls, 1)

    def test_refresh_bypasses_the_cache(self):
        self._list()
        self._list(refresh=["1"])
        self.assertEqual(self.calls, 2)

    def test_different_query_is_a_different_cache_entry(self):
        self._list()
        self._list(q=["song"])
        self.assertEqual(self.calls, 2)

    def test_unreachable_peer_falls_back_to_last_good_copy(self):
        self._list()
        handlers_media_browse._source_cache.fresh_seconds = 0
        self.addCleanup(setattr, handlers_media_browse._source_cache, "fresh_seconds", 120.0)
        self.fail = True
        body = self._list()
        status = {s["drone_id"]: s for s in body["sources"]}["drone-ok"]
        self.assertEqual(status["status"], "cached")
        self.assertIn("cached_age_seconds", status)
        self.assertIn("drone-ok", {i["drone_id"] for i in body["items"]})

    def test_never_seen_unreachable_peer_is_offline_not_cached(self):
        self.fail = True
        body = self._list()
        self.assertEqual({s["drone_id"]: s["status"] for s in body["sources"]}["drone-ok"], "offline")

    def test_cache_is_bounded_and_expires(self):
        clock = [0.0]
        cache = media_browse.SourceCache(fresh_seconds=10, stale_seconds=100, max_entries=2, clock=lambda: clock[0])
        cache.put(("a", 1), {"x": 1})
        cache.put(("b", 1), {"x": 2})
        cache.put(("c", 1), {"x": 3})
        self.assertIsNone(cache.get(("a", 1), allow_stale=True))
        clock[0] = 50
        self.assertIsNone(cache.get(("b", 1)))
        self.assertIsNotNone(cache.get(("b", 1), allow_stale=True))
        clock[0] = 500
        self.assertIsNone(cache.get(("b", 1), allow_stale=True))
        cache.put(("c", 2), {"x": 4})
        self.assertEqual(cache.invalidate("c"), 2)


class InPlaceLoadingBackendTests(unittest.TestCase):
    """The pages ask for one Drone at a time, peers only, and local fingerprints flag duplicates."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = _settings(self.root)
        track = self.root / "music" / "A" / "Song.mp3"
        track.parent.mkdir(parents=True)
        track.write_bytes(b"x" * 64)
        music_store.sync_music_cache(self.settings.music_root)
        self.local_fp = music_store.list_music(self.settings.music_root)[0]["fingerprint"]
        handlers_media_browse._source_cache.invalidate()
        handlers_media_browse._fingerprint_cache.clear()
        self.asked = []
        peers = [{"drone_id": "drone-a", "name": "A"}, {"drone_id": "drone-b", "name": "B"}]

        def peer_request(inner_self, peer, peer_id, kind, query_params, overrides=None):
            self.asked.append(peer_id)
            return {"total": 2, "items": [{"entry_key": "01", "display_title": "Same", "fingerprint": self.local_fp},
                                          {"entry_key": "02", "display_title": "New", "fingerprint": "zz"}]}

        for patcher in (
            mock.patch("app.transfer.local_network.is_local_mode", return_value=True),
            mock.patch("app.transfer.local_network.paired_peers", return_value=peers),
            mock.patch("app.transfer.local_network.get_paired_peer", side_effect=lambda s, pid: next((p for p in peers if p["drone_id"] == pid), None)),
            mock.patch.object(HandlersMediaBrowseMixin, "_peer_inventory_request", peer_request, create=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        handlers_media_browse._source_cache.invalidate()
        self._tmp.cleanup()

    def test_sources_lists_local_first_without_any_network(self):
        handler = _Handler(self.settings)
        handler._handle_media_browse_sources()
        status, body = handler.json
        self.assertEqual(status, 200)
        self.assertEqual([s["drone_id"] for s in body["sources"]], ["drone-local", "drone-a", "drone-b"])
        self.assertTrue(body["sources"][0]["is_local"])
        self.assertEqual(self.asked, [])

    def test_one_drone_peers_only_request_asks_only_that_drone(self):
        handler = _Handler(self.settings)
        handler._handle_media_browse_list("music", {"drones": ["drone-b"], "exclude_local": ["1"]})
        body = handler.json[1]
        self.assertEqual(self.asked, ["drone-b"])
        self.assertEqual([s["drone_id"] for s in body["sources"]], ["drone-b"])
        self.assertFalse(any(item["is_local"] for item in body["items"]))

    def test_items_already_on_this_drone_are_flagged(self):
        handler = _Handler(self.settings)
        handler._handle_media_browse_list("music", {"drones": ["drone-a"], "exclude_local": ["1"]})
        flags = {item["entry_key"]: item["exists_locally"] for item in handler.json[1]["items"]}
        self.assertEqual(flags, {"01": True, "02": False})

    def test_missing_file_is_a_clear_500_not_a_bare_404(self):
        with mock.patch.object(media_browse, "collect_page", side_effect=FileNotFoundError("gone")):
            handler = _Handler(self.settings)
            handler._handle_media_browse_list("music", {})
        self.assertEqual(handler.json[0], 500)
        self.assertIn("media browse failed", handler.json[1]["error"])


class CopyHereTests(unittest.TestCase):
    def test_music_copy_uses_the_normal_download_manager(self):
        from app.web.handlers_network import HandlersNetworkMixin

        manager = mock.Mock()
        manager.enqueue_music.return_value = {"id": "j"}
        handler = HandlersNetworkMixin()
        jobs = handler._enqueue_local_asset(
            manager, {"network_mode": "local_network"}, {"drone_id": "p"}, "music",
            {"file_path": "A/B/01.mp3", "file_size": 5, "fingerprint": "fp"},
        )
        self.assertEqual(jobs, [{"id": "j"}])
        kwargs = manager.enqueue_music.call_args.kwargs
        self.assertEqual(kwargs["expected_fingerprint"], "fp")
        self.assertEqual(manager.enqueue_music.call_args.args[2], "A/B/01.mp3")

    def test_game_copy_queues_rom_and_its_artwork_like_transfers(self):
        from app.web.handlers_network import HandlersNetworkMixin

        class H(HandlersNetworkMixin):
            def _local_rom_index(self, system, cache=None):
                return {}

            def _match_local_rom(self, index, item):
                return None

            def _local_artwork_index(self, system, cache=None):
                return {}

            def _ensure_system_writable_once(self, system):
                pass

        manager = mock.Mock()
        manager.find_pending_rom.return_value = None
        manager.enqueue_rom.return_value = {"id": "rom"}
        manager.enqueue_artwork.return_value = {"id": "art"}
        item = {"system": "snes", "relative_path": "Game.sfc", "fingerprint": "f", "gamelist": {"image": "./media/images/Game.png"}}
        jobs = H()._enqueue_local_asset(manager, {}, {"drone_id": "p"}, "roms", item, include_artwork=True)
        self.assertEqual([j["id"] for j in jobs], ["rom", "art"])
        manager.enqueue_rom.assert_called_once()
        self.assertEqual(manager.enqueue_artwork.call_args.args[2:5], ("snes", "Game.sfc", "image"))


class MusicDownloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = _settings(self.root)
        (self.root / "music").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _fetch(self, body, **kwargs):
        from app.transfer import peer_download

        response = _FakeResponse(body, headers={"Content-Length": str(len(body))})
        with mock.patch.object(peer_download, "_peer_open_stream_for_peer", return_value=(response, "http://p")):
            return peer_download._download_music_from_peer(
                self.settings, {}, {"drone_id": "p"}, "Artist/Album/01.mp3", **kwargs
            )

    def test_download_installs_file_and_verifies_fingerprint(self):
        source = self.root / "src.mp3"
        source.write_bytes(b"abc" * 50)
        expected = music_store.build_music_fingerprint(source)
        result = self._fetch(b"abc" * 50, expected_size=150, expected_fingerprint=expected)
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.root / "music" / "Artist" / "Album" / "01.mp3").read_bytes(), b"abc" * 50)

    def test_fingerprint_mismatch_leaves_nothing_behind(self):
        with self.assertRaises(RuntimeError):
            self._fetch(b"abc" * 50, expected_fingerprint="deadbeef")
        album = self.root / "music" / "Artist" / "Album"
        self.assertEqual(list(album.glob("*")), [])

    def test_existing_file_is_skipped_unless_overwrite(self):
        target = self.root / "music" / "Artist" / "Album" / "01.mp3"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old")
        self.assertEqual(self._fetch(b"new")["status"], "skipped")
        self.assertEqual(target.read_bytes(), b"old")

    def test_traversal_is_rejected(self):
        from app.transfer import peer_download

        with self.assertRaises(ValueError):
            peer_download._download_music_from_peer(self.settings, {}, {"drone_id": "p"}, "../evil.mp3")


class UiContractTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1] / "app" / "web"

    def _script(self):
        return (self.ROOT / "static" / "js" / "media_browse.js").read_text(encoding="utf-8")

    def test_media_browse_script_contract(self):
        script = self._script()
        for needle in ("remote-media-layer", "MutationObserver", "DroneTransferToast", "LoadToast", "escapeHtml",
                       "themed-modal", "/admin/local-network/sync", "include_artwork: true", "transfer_speed_bps",
                       "downloaded_bytes", "exclude_local", "also-on", "onRouteRendered", "requestAnimationFrame"):
            self.assertIn(needle, script)
        for forbidden in ("onclick=", "onerror=", "eval(", "location.reload"):
            self.assertNotIn(forbidden, script)

    def test_there_is_no_separate_all_drones_page_or_button(self):
        router = (self.ROOT / "static" / "js" / "drone.js").read_text(encoding="utf-8")
        self.assertNotIn('startsWith("#network")', router)
        self.assertNotIn("renderNetworkMediaPage", router)
        self.assertNotIn("All Drones", router)
        self.assertNotIn("renderNetworkMediaPage", self._script())

    def test_script_is_loaded_and_detail_pages_are_hooked(self):
        self.assertIn("/static/js/media_browse.js", (self.ROOT / "templates" / "index.html").read_text(encoding="utf-8"))
        router = (self.ROOT / "static" / "js" / "drone.js").read_text(encoding="utf-8")
        self.assertIn("DroneRemoteMedia.onRouteRendered(hash)", router)

    def test_api_calls_do_not_double_the_api_prefix(self):
        # drone.js api()/apiPost() already prepend /v1/api; passing the full path 404s ("not found").
        script = self._script()
        self.assertNotIn("api(`${BASE}", script)
        self.assertNotIn("apiPost(`${BASE}", script)
        self.assertNotIn('api("/v1/api', script)
        self.assertIn('const API = "/media-browse"', script)

    def test_loading_is_reported_in_a_toast_and_one_request_per_drone(self):
        script = self._script()
        self.assertIn("LoadToast.begin(kind, list.length)", script)
        self.assertIn("drones: entry.id", script)  # one request per Drone, so a slow one never blocks the rest

    def test_transfer_toast_has_no_dismiss_while_running(self):
        self.assertIn('classList.toggle("d-none", !finished)', self._script())


if __name__ == "__main__":
    unittest.main()
