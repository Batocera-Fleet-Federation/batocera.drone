"""Movies P2P transfer: the peer-serve handler, the peer download function,
peer inventory listing, and local-network sync dispatch.

Movies are a flat asset type (no system/artwork association, unlike
ROMs/BIOS/saves) -- these tests mirror the BIOS/ROM peer-transfer tests but
verify the flat shape and the sampled-fingerprint integrity check (movies use
the same sampled hash as ROMs/saves, not BIOS's full-file MD5)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.drone_api import RomRepository
from app.roms.rom_scanner import _poll_rom_metadata_once
from app.storage import movies_store
from app.transfer.download_manager import DownloadManager
from app.transfer.peer_download import _download_movie_from_peer
from app.web import handlers_peer


def _settings(root: Path) -> Settings:
    with mock.patch.dict(
        "os.environ",
        {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "MOVIES_ROOT": str(root / "movies"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "movies-test-device",
        },
        clear=True,
    ):
        return Settings.from_env()


class DownloadMovieFromPeerTests(unittest.TestCase):
    def test_happy_path_streams_to_movies_root_and_verifies_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = _settings(root)
            pinned_cert = Path(tmp) / "peer-cert.pem"
            pinned_cert.write_text("peer-cert", encoding="utf-8")

            content = b"movie-bytes" * 100

            # Compute the expected fingerprint the same way the source would.
            src_file = Path(tmp) / "source.mp4"
            src_file.write_bytes(content)
            expected_fp = movies_store.build_movie_fingerprint(src_file)

            class FakeResponse:
                def __init__(self, data: bytes):
                    self._chunks = [data, b""]

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self, _size=-1):
                    return self._chunks.pop(0)

                headers = {}

            requests = []

            def fake_context(_settings, url, verify=False, cafile=None):
                return object()

            def fake_urlopen(request, timeout=None, context=None):
                requests.append(request.full_url)
                return FakeResponse(content)

            peer = {
                "drone_id": "bff-drone-b",
                "reachable_url": "https://bff-drone-b:443",
            }
            with mock.patch(
                "app.transfer.peer_download._peer_trust_cafile", return_value=pinned_cert
            ), mock.patch(
                "app.transfer.peer_download._drone_client_ssl_context", side_effect=fake_context
            ), mock.patch(
                "app.transfer.peer_download.urlopen", side_effect=fake_urlopen
            ):
                result = _download_movie_from_peer(
                    settings, {}, peer, "clips/Vacation.mp4",
                    expected_size=len(content), expected_fingerprint=expected_fp,
                )

            self.assertEqual(requests, ["https://bff-drone-b:443/v1/api/peer/movies/clips/Vacation.mp4"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["asset_type"], "movies")
            self.assertEqual(result["file_type"], "Movie")
            self.assertEqual(result["movie_name"], "clips/Vacation.mp4")
            self.assertEqual(result["fingerprint"], expected_fp)
            written = root / "movies" / "clips" / "Vacation.mp4"
            self.assertEqual(written.read_bytes(), content)

    def test_fingerprint_mismatch_raises_and_cleans_up_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = _settings(root)
            pinned_cert = Path(tmp) / "peer-cert.pem"
            pinned_cert.write_text("peer-cert", encoding="utf-8")

            class FakeResponse:
                def __init__(self, data: bytes):
                    self._chunks = [data, b""]

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self, _size=-1):
                    return self._chunks.pop(0)

                headers = {}

            peer = {"drone_id": "bff-drone-b", "reachable_url": "https://bff-drone-b:443"}
            with mock.patch(
                "app.transfer.peer_download._peer_trust_cafile", return_value=pinned_cert
            ), mock.patch(
                "app.transfer.peer_download._drone_client_ssl_context", return_value=object()
            ), mock.patch(
                "app.transfer.peer_download.urlopen", return_value=FakeResponse(b"corrupted-data")
            ):
                with self.assertRaises(RuntimeError):
                    _download_movie_from_peer(
                        settings, {}, peer, "clips/Vacation.mp4",
                        expected_fingerprint="0" * 40,
                    )
            self.assertFalse((root / "movies" / "clips" / "Vacation.mp4").exists())
            self.assertFalse((root / "movies" / "clips" / "Vacation.mp4.part").exists())

    def test_rejects_target_path_escaping_movies_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = _settings(root)
            peer = {"drone_id": "bff-drone-b", "reachable_url": "https://bff-drone-b:443"}
            with self.assertRaises(ValueError):
                _download_movie_from_peer(settings, {}, peer, "../../etc/passwd")


class _FakePeerHandler:
    """Minimal stand-in for RomRequestHandler's send/stream/log surface,
    mirroring the _FakeHandler pattern used for the VPN handler mixin tests."""

    def __init__(self, settings: Settings, *, authorized: bool = True) -> None:
        self.settings = settings
        self._authorized = authorized
        self.response = None
        self.streamed = None

    def _peer_request_authorized(self) -> bool:
        return self._authorized

    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.response = (status_code, payload)

    def _stream_file(self, path, content_type, as_attachment=False, **kwargs) -> None:
        self.streamed = {"path": path, "content_type": content_type, "as_attachment": as_attachment}

    def log_error(self, *args, **kwargs) -> None:
        pass

    def log_message(self, *args, **kwargs) -> None:
        pass


def _peer_handler(settings: Settings, **kwargs) -> _FakePeerHandler:
    # _FakePeerHandler first so its _stream_file/_send_json stubs win over
    # HandlersPeerMixin's real (BaseHTTPRequestHandler-dependent) versions.
    class Handler(_FakePeerHandler, handlers_peer.HandlersPeerMixin):
        pass

    return Handler(settings, **kwargs)


class HandlePeerMovieDownloadTests(unittest.TestCase):
    def test_rejects_unauthorized_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            handler = _peer_handler(settings, authorized=False)
            handler._handle_peer_movie_download("clips/A.mp4")
            self.assertIsNone(handler.response)
            self.assertIsNone(handler.streamed)

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            handler = _peer_handler(settings)
            handler._handle_peer_movie_download("../../etc/passwd")
            self.assertEqual(handler.response[0], 400)

    def test_404_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            handler = _peer_handler(settings)
            handler._handle_peer_movie_download("clips/does-not-exist.mp4")
            self.assertEqual(handler.response[0], 404)

    def test_streams_existing_movie_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            movie_path = settings.movies_root / "clips" / "A.mp4"
            movie_path.parent.mkdir(parents=True)
            movie_path.write_bytes(b"movie-bytes")
            handler = _peer_handler(settings)
            handler._handle_peer_movie_download("clips/A.mp4")
            self.assertIsNone(handler.response)
            self.assertEqual(handler.streamed["path"], movie_path.resolve())
            self.assertTrue(handler.streamed["as_attachment"])


class CollectPeerInventoryMoviesTests(unittest.TestCase):
    def test_movies_inventory_pages_flat_with_no_system_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            for name in ("Alpha.mp4", "Beta.mp4"):
                path = settings.movies_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode())
            movies_store.sync_movies_cache(settings.movies_root)
            handler = _peer_handler(settings)

            payload = handler._collect_peer_inventory("movies", {})
            self.assertEqual(payload["total"], 2)
            self.assertEqual(payload["asset_type"], "movies")
            for item in payload["items"]:
                self.assertNotIn("system", item)


class EnqueueLocalMovieAssetTests(unittest.TestCase):
    def test_enqueue_local_asset_movies_branch_calls_enqueue_movie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from app.web import handlers_network

            settings = _settings(Path(tmp) / "userdata")

            class Handler(handlers_network.HandlersNetworkMixin):
                def __init__(self, settings):
                    self.settings = settings

            handler = Handler(settings)
            manager = mock.create_autospec(DownloadManager, instance=True)
            manager.enqueue_movie.return_value = {"id": "job-1", "asset_type": "movies"}
            item = {
                "file_path": "clips/Vacation.mp4",
                "file_size": 4096,
                "movies_fingerprint": "abc123",
            }

            jobs = handler._enqueue_local_asset(manager, {}, {"drone_id": "peer-1"}, "movies", item)

            manager.enqueue_movie.assert_called_once_with(
                {}, {"drone_id": "peer-1"}, "clips/Vacation.mp4",
                expected_size=4096, expected_fingerprint="abc123",
                overwrite=False,
            )
            self.assertEqual(jobs, [{"id": "job-1", "asset_type": "movies"}])


class MoviesCacheSyncedByRealPollCycleTests(unittest.TestCase):
    """Regression test for a real bug: on a real device (use_fake_data=False),
    sync_movies_cache() was only ever called from _collect_peer_inventory's
    use_fake_data branch -- never in production -- so movies_cache_entries
    stayed empty forever regardless of how many real files were on disk, and
    every peer inventory request correctly-but-uselessly reported zero movies.
    Fixed by syncing movies in _poll_rom_metadata_once, the same always-on
    poll cycle that already keeps the saves cache warm.
    """

    def test_poll_cycle_populates_movies_cache_without_fake_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            (root / "roms").mkdir(parents=True)
            (root / "bios").mkdir(parents=True)
            movie = root / "movies" / "Alien.mp4"
            movie.parent.mkdir(parents=True)
            movie.write_bytes(b"movie-bytes")
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "SAVES_ROOT": str(root / "saves"),
                    "MOVIES_ROOT": str(root / "movies"),
                    "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                    "DRONE_DEVICE_ID": "movies-poll-test",
                },
                clear=True,
            ):
                settings = Settings.from_env()
            self.assertFalse(settings.use_fake_data)

            # Before the fix this stays {"total": 0, "items": []} forever, no
            # matter how many real files are on disk, because nothing ever
            # calls sync_movies_cache() outside of use_fake_data.
            self.assertEqual(movies_store.list_movies_page(settings.movies_root)["total"], 0)

            _poll_rom_metadata_once(settings, RomRepository(settings.roms_root, settings.bios_root))

            page = movies_store.list_movies_page(settings.movies_root)
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["items"][0]["movie_name"], "Alien.mp4")

            # And the exact path the user hit -- a paired peer's inventory
            # request -- now sees it too.
            handler = _peer_handler(settings)
            payload = handler._collect_peer_inventory("movies", {})
            self.assertEqual(payload["total"], 1)


if __name__ == "__main__":
    unittest.main()
