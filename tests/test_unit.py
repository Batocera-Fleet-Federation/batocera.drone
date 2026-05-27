import base64
import io
import json
import socket
import subprocess
import tempfile
import unittest
from threading import Event
from unittest import mock
from pathlib import Path

from app.mock_data import seed_mock_userdata
from app.drone_api import (
    FAKE_OVERMIND_EMAIL,
    FAKE_OVERMIND_TOKEN,
    BasicAuth,
    DownloadCancelled,
    DownloadManager,
    DroneCredentialStore,
    LaunchBoxClient,
    RomRepository,
    Settings,
    _clean_rom_title,
    _drone_reachable_url,
    _drone_report_host,
    _peer_address,
    _get_local_ip_addresses,
    _get_router_ip_address,
    _collect_gpu_info,
    _format_overmind_error,
    _launchbox_platform_for_system,
    _load_overmind_config_for_settings,
    _collect_rom_metadata,
    _hash_rom_metadata_batches,
    _poll_rom_metadata_cache,
    _poll_rom_metadata_once,
    _rom_metadata_cache_path,
    _sync_rom_metadata_to_overmind,
    _write_json_file,
    _sample_speed,
    _real_data_roots,
    _peer_ssl_diagnostic,
    _peer_trust_cafile,
    _download_rom_from_peer,
    _download_rom_folder_from_peer,
    _collision_safe_target,
    _rom_md5_exists,
    _best_peer_for_rom,
    _execute_overmind_action,
    _register_or_claim_overmind_token,
    _reclaim_overmind_token_after_unauthorized,
    _collect_emulator_configs,
    _commit_emulator_config_fingerprints,
    _collect_log_sources,
    _commit_log_cursors,
    _collect_game_logs,
)
from urllib.error import HTTPError, URLError


class BasicAuthTests(unittest.TestCase):
    def test_check_valid_header(self) -> None:
        auth = BasicAuth("admin", "changeme")
        token = base64.b64encode(b"admin:changeme").decode("ascii")
        self.assertTrue(auth.check(f"Basic {token}"))

    def test_check_invalid_header(self) -> None:
        auth = BasicAuth("admin", "changeme")
        token = base64.b64encode(b"admin:wrong").decode("ascii")
        self.assertFalse(auth.check(f"Basic {token}"))
        self.assertFalse(auth.check(None))

    def test_default_drone_credentials_and_hashed_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DroneCredentialStore(Path(tmp) / "credentials.json")
            auth = BasicAuth(None, None, credential_store=store)
            default_token = base64.b64encode(b"batocera:linux").decode("ascii")
            self.assertTrue(auth.check(f"Basic {default_token}"))

            result = store.update("arcade-admin", "BetterPass123")
            self.assertTrue(result["stored"])
            saved = (Path(tmp) / "credentials.json").read_text(encoding="utf-8")
            self.assertIn("password_hash", saved)
            self.assertNotIn("BetterPass123", saved)
            self.assertFalse(auth.check(f"Basic {default_token}"))
            updated_token = base64.b64encode(b"arcade-admin:BetterPass123").decode("ascii")
            self.assertTrue(auth.check(f"Basic {updated_token}"))


class SettingsTests(unittest.TestCase):
    def test_overmind_error_format_includes_class_when_message_is_blank(self) -> None:
        self.assertEqual(_format_overmind_error(TimeoutError()), "TimeoutError()")
        self.assertIn("URLError reason=", _format_overmind_error(URLError(TimeoutError())))

    def test_hostname_override_builds_reported_drone_url(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"HOSTNAME_OVERRIDE": "bff-drone-a", "HTTPS_PORT": "8443"},
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.hostname_override, "bff-drone-a")
        self.assertEqual(_drone_reachable_url(settings, {"ipv4": ["192.168.1.50"]}), "https://bff-drone-a:8443")

    def test_host_preference_order_is_override_ipv4_ipv6(self) -> None:
        with mock.patch.dict("os.environ", {"HOSTNAME_OVERRIDE": "bff-drone-a", "HTTPS_PORT": "8443"}, clear=True):
            settings = Settings.from_env()
        network = {"ipv4": ["192.168.1.50"], "ipv6": ["fd00::50"]}
        self.assertEqual(_drone_report_host(settings, network), "bff-drone-a")

        with mock.patch.dict("os.environ", {"HTTPS_PORT": "8443"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(_drone_report_host(settings, network), "192.168.1.50")
        self.assertEqual(_drone_report_host(settings, {"ipv6": ["fd00::50"]}), "fd00::50")
        self.assertEqual(_drone_reachable_url(settings, {"ipv6": ["fd00::50"]}), "https://[fd00::50]:8443")

    def test_ipv6_route_failure_is_quiet_without_debug(self) -> None:
        real_socket = socket.socket

        def fake_socket(family, *args, **kwargs):
            if family == socket.AF_INET6:
                raise OSError("No route to host")
            return real_socket(family, *args, **kwargs)

        with mock.patch("app.drone_api.socket.socket", side_effect=fake_socket), mock.patch("builtins.print") as printed:
            network = _get_local_ip_addresses()

        self.assertIn("127.0.0.1", network["ipv4"])
        self.assertFalse(any("IPv6 route resolution failed" in str(call) for call in printed.mock_calls))

    def test_router_ip_address_uses_route_fallback(self) -> None:
        results = [
            mock.Mock(stdout=""),
            mock.Mock(stdout="192.168.50.1\n"),
        ]

        with mock.patch("app.drone_api.subprocess.run", side_effect=results) as run:
            self.assertEqual(_get_router_ip_address(), "192.168.50.1")

        self.assertEqual(run.call_count, 2)

    def test_peer_address_uses_reachable_url_before_ips(self) -> None:
        peer = {
            "reachable_url": "https://bff-drone-b:8443",
            "resolved_network": {"ipv4": ["172.20.0.4"], "ipv6": ["fd00::4"]},
            "api_port": 8443,
        }
        self.assertEqual(_peer_address(peer), "https://bff-drone-b:8443")

    def test_peer_address_prefers_public_endpoint_for_remote_swarm_transfers(self) -> None:
        peer = {
            "public_reachable_url": "https://198.51.100.20:8443",
            "public_ip": "198.51.100.20",
            "reachable_url": "https://192.168.1.20:8443",
            "resolved_network": {"ipv4": ["192.168.1.20"]},
            "api_port": 8443,
        }
        self.assertEqual(_peer_address(peer), "https://198.51.100.20:8443")

    def test_peer_address_builds_public_endpoint_from_public_ip(self) -> None:
        peer = {
            "public_ip": "198.51.100.21",
            "reachable_url": "https://192.168.1.21:8443",
            "scheme": "https",
            "api_port": 8443,
        }
        self.assertEqual(_peer_address(peer), "https://198.51.100.21:8443")

    def test_log_source_collection_sends_only_new_log_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir(parents=True)
            stdout_log = log_dir / "stdout.log"
            stdout_log.write_text("first\nsecond\n", encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "LOG_DIR": str(log_dir)},
                clear=True,
            ):
                settings = Settings.from_env()

            first = _collect_log_sources(settings)
            stdout_entry = next(row for row in first["logs"] if row["source"] == "drone_stdout")
            self.assertEqual(stdout_entry["files"][0]["content"], "first\nsecond\n")

            unacknowledged = _collect_log_sources(settings)
            stdout_entry = next(row for row in unacknowledged["logs"] if row["source"] == "drone_stdout")
            self.assertEqual(stdout_entry["files"][0]["content"], "first\nsecond\n")

            _commit_log_cursors(settings, first["_cursors"])
            with stdout_log.open("a", encoding="utf-8") as handle:
                handle.write("third\n")

            second = _collect_log_sources(settings)
            stdout_entry = next(row for row in second["logs"] if row["source"] == "drone_stdout")
            self.assertEqual(stdout_entry["files"][0]["content"], "third\n")
            _commit_log_cursors(settings, second["_cursors"])
            self.assertEqual(_collect_log_sources(settings)["logs"], [])

            stdout_log.write_text("rewritten\n", encoding="utf-8")
            rewritten = _collect_log_sources(settings)
            stdout_entry = next(row for row in rewritten["logs"] if row["source"] == "drone_stdout")
            self.assertEqual(stdout_entry["files"][0]["content"], "rewritten\n")

    def test_log_source_collection_skips_old_bytes_when_backlog_is_large(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir(parents=True)
            stdout_log = log_dir / "stdout.log"
            stdout_log.write_text("old-line\n" * 40000 + "latest-checkpoint\n", encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "LOG_DIR": str(log_dir)},
                clear=True,
            ):
                settings = Settings.from_env()

            payload = _collect_log_sources(settings)
            file_info = next(row for row in payload["logs"] if row["source"] == "drone_stdout")["files"][0]

            self.assertGreater(file_info["skipped_bytes"], 0)
            self.assertIn("older buffered bytes to show current output", file_info["content"])
            self.assertIn("latest-checkpoint", file_info["content"])
            self.assertEqual(payload["_cursors"][str(stdout_log.resolve())]["size"], stdout_log.stat().st_size)

    def test_game_log_collection_detects_launch_with_md5(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            roms_root = root / "roms"
            rom = roms_root / "snes" / "Game.sfc"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"rom-data")
            launch_log = root / "system" / "logs" / "es_launch_stdout.log"
            launch_log.parent.mkdir(parents=True)
            launch_log.write_text(
                f"2026-05-26 10:15:00 emulator=snes\n2026-05-26 10:15:00 rom={rom}\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(roms_root)},
                clear=True,
            ):
                settings = Settings.from_env()

            result = _collect_game_logs(settings, RomRepository(roms_root, root / "bios"))
            self.assertEqual(len(result["sessions"]), 1)
            session = result["sessions"][0]
            self.assertEqual(session["system_name"], "snes")
            self.assertEqual(session["game_name"], "Game.sfc")
            self.assertEqual(session["rom_path"], rom.resolve().as_posix())
            self.assertEqual(session["rom_md5"], RomRepository.build_md5(rom))
            self.assertEqual(session["played_at"], "2026-05-26T10:15:00+00:00")

    def test_emulator_config_collection_sends_changed_configs_and_skips_bak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            configs_root = root / "system" / "configs" / "retroarch"
            configs_root.mkdir(parents=True)
            config = configs_root / "retroarch.cfg"
            backup = configs_root / "retroarch.cfg.bak"
            config.write_text("video_driver = gl", encoding="utf-8")
            backup.write_text("old", encoding="utf-8")
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()

            first = _collect_emulator_configs(settings)
            self.assertEqual([row["relative_path"] for row in first["configs"]], ["retroarch/retroarch.cfg"])

            unacknowledged = _collect_emulator_configs(settings)
            self.assertEqual([row["relative_path"] for row in unacknowledged["configs"]], ["retroarch/retroarch.cfg"])

            _commit_emulator_config_fingerprints(settings, first["_fingerprints"])
            second = _collect_emulator_configs(settings)
            self.assertEqual(second["configs"], [])
            self.assertFalse(second["changed"])
            requested_snapshot = _collect_emulator_configs(settings, include_unchanged=True)
            self.assertEqual([row["relative_path"] for row in requested_snapshot["configs"]], ["retroarch/retroarch.cfg"])
            self.assertFalse(requested_snapshot["incremental"])

            legacy_state = root / "system" / "drone-app" / "overmind_config_fingerprints.json"
            legacy_state.write_text(json.dumps(first["_fingerprints"]), encoding="utf-8")
            legacy_retry = _collect_emulator_configs(settings)
            self.assertEqual([row["relative_path"] for row in legacy_retry["configs"]], ["retroarch/retroarch.cfg"])
            _commit_emulator_config_fingerprints(settings, legacy_retry["_fingerprints"])

            config.write_text("video_driver = vulkan", encoding="utf-8")
            third = _collect_emulator_configs(settings)
            self.assertEqual([row["relative_path"] for row in third["configs"]], ["retroarch/retroarch.cfg"])
            self.assertIn("vulkan", third["configs"][0]["content"])

    def test_peer_trust_prefers_configured_ca_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ca_file = Path(tmp) / "ca.crt"
            ca_file.write_text("test-ca", encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(Path(tmp) / "userdata"), "DRONE_MTLS_CA_FILE": str(ca_file)},
                clear=True,
            ):
                settings = Settings.from_env()

            with mock.patch("app.drone_api._fetch_peer_certificate") as fetch:
                self.assertEqual(_peer_trust_cafile(settings, peer_id="bff-drone-b", config={}), ca_file)
                fetch.assert_not_called()

    def test_peer_ssl_diagnostic_identifies_hostname_mismatch(self) -> None:
        diagnostic = _peer_ssl_diagnostic(
            "https://bff-drone-b:8443/v1/api/peer/health",
            Path("/tmp/local-ca.crt"),
            Exception("Hostname mismatch, certificate is not valid for 'bff-drone-b'"),
        )
        self.assertIn("hostname/SAN mismatch", diagnostic)
        self.assertIn("hostname=bff-drone-b", diagnostic)
        self.assertIn("cafile=/tmp/local-ca.crt", diagnostic)

    def test_download_rom_from_peer_uses_configured_ca_bundle_and_reachable_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            ca_file = Path(tmp) / "ca.crt"
            ca_file.write_text("test-ca", encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "DRONE_MTLS_CA_FILE": str(ca_file),
                },
                clear=True,
            ):
                settings = Settings.from_env()

            class FakeResponse:
                def __init__(self):
                    self._chunks = [b"ROMDATA", b""]

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self, _size=-1):
                    return self._chunks.pop(0)

            contexts = []
            requests = []

            def fake_context(_settings, url, verify=False, cafile=None):
                contexts.append((url, verify, cafile))
                return object()

            def fake_urlopen(request, timeout=None, context=None):
                requests.append((request.full_url, timeout, context))
                return FakeResponse()

            peer = {
                "drone_id": "bff-drone-b",
                "reachable_url": "https://bff-drone-b:8443",
                "resolved_network": {"ipv4": ["172.20.0.4"]},
            }
            with mock.patch("app.drone_api._drone_client_ssl_context", side_effect=fake_context), mock.patch(
                "app.drone_api.urlopen", side_effect=fake_urlopen
            ), mock.patch("app.drone_api._fetch_peer_certificate") as fetch:
                result = _download_rom_from_peer(settings, {}, peer, "atari7800", "Asteroids (USA).zip", expected_size=7)

            self.assertEqual(result["source_drone_id"], "bff-drone-b")
            self.assertEqual(requests[0][0], "https://bff-drone-b:8443/v1/api/peer/roms/atari7800/Asteroids%20%28USA%29.zip")
            self.assertEqual(contexts[0][1], True)
            self.assertEqual(contexts[0][2], ca_file)
            self.assertEqual((root / "roms" / "atari7800" / "Asteroids (USA).zip").read_bytes(), b"ROMDATA")
            fetch.assert_not_called()

    def test_cancelled_download_uses_part_file_and_is_not_inventoried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()

            class FakeResponse:
                headers = {"Content-Length": "14"}

                def __init__(self):
                    self._chunks = [b"PARTIAL", b"ROMDATA", b""]

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self, _size=-1):
                    return self._chunks.pop(0)

            cancel = Event()

            def progress(_downloaded, _total):
                cancel.set()

            peer = {"drone_id": "source-a", "reachable_url": "http://source-a:8080"}
            with mock.patch("app.drone_api.urlopen", return_value=FakeResponse()):
                with self.assertRaises(DownloadCancelled):
                    _download_rom_from_peer(
                        settings,
                        {},
                        peer,
                        "snes",
                        "Cancel Me.zip",
                        progress_callback=progress,
                        cancellation_event=cancel,
                    )

            repo = RomRepository(root / "roms", root / "bios")
            self.assertFalse((root / "roms" / "snes" / "Cancel Me.zip").exists())
            self.assertFalse((root / "roms" / "snes" / "Cancel Me.zip.part").exists())
            self.assertEqual(repo.list_assets("snes", "roms")[1], [])

    def test_download_folder_rom_from_peer_recreates_tree_without_md5(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()

            manifest = {
                "relative_path": "Game.ps3",
                "entry_type": "folder",
                "file_size": 10,
                "directories": ["PS3_GAME", "PS3_GAME/USRDIR"],
                "files": [
                    {"relative_path": "PS3_GAME/PARAM.SFO", "file_size": 5},
                    {"relative_path": "PS3_GAME/USRDIR/EBOOT.BIN", "file_size": 5},
                ],
            }

            class FakeResponse:
                def __init__(self, data):
                    self._chunks = [data, b""]
                    self.headers = {"Content-Length": str(len(data))}

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self, _size=-1):
                    return self._chunks.pop(0)

            def fake_urlopen(request, timeout=None, context=None):
                url = request.full_url
                if url.endswith("/PS3_GAME/PARAM.SFO"):
                    return FakeResponse(b"param")
                if url.endswith("/PS3_GAME/USRDIR/EBOOT.BIN"):
                    return FakeResponse(b"eboot")
                raise AssertionError(url)

            peer = {"drone_id": "source-a", "reachable_url": "http://source-a:8080"}
            with mock.patch("app.drone_api._peer_get_json", return_value=manifest), mock.patch(
                "app.drone_api.urlopen", side_effect=fake_urlopen
            ), mock.patch.object(RomRepository, "build_md5", side_effect=AssertionError("folder sync should not hash")):
                result = _download_rom_folder_from_peer(settings, {}, peer, "ps3", "Game.ps3", expected_size=10)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["entry_type"], "folder")
            self.assertEqual(result["bytes_transferred"], 10)
            self.assertEqual((root / "roms" / "ps3" / "Game.ps3" / "PS3_GAME" / "PARAM.SFO").read_bytes(), b"param")
            self.assertEqual((root / "roms" / "ps3" / "Game.ps3" / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").read_bytes(), b"eboot")

    def test_download_manager_tracks_queue_and_idempotent_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "OVERMIND_DEVICE_ID": "target-a",
                },
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(root / "roms", root / "bios")
            with mock.patch("app.drone_api.Thread.start"):
                manager = DownloadManager(settings, repo)

            first = manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "One.zip")
            second = manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "Two.zip")
            snapshot = manager.snapshot()
            self.assertEqual([job["queue_position"] for job in snapshot["queued"]], [1, 2])
            self.assertEqual(first["target_drone_id"], "target-a")
            self.assertEqual(second["source_drone_id"], "source-a")

            result = manager.cancel(second["job_id"])
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(manager.cancel(second["job_id"])["status"], "cancelled")
            snapshot = manager.snapshot()
            self.assertEqual(len(snapshot["queued"]), 1)
            self.assertEqual(snapshot["recent"][0]["status"], "cancelled")

    def test_download_manager_pushes_terminal_sync_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "OVERMIND_DEVICE_ID": "target-a",
                },
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(root / "roms", root / "bios")
            with mock.patch("app.drone_api.Thread.start"):
                manager = DownloadManager(settings, repo)

            config = {"overmind_url": "https://overmind.local", "overmind_token": "drone-token"}
            queued = manager.enqueue_rom(config, {"drone_id": "source-a"}, "snes", "Game.zip", expected_size=8, expected_md5="abc")
            completed = {
                "source_drone_id": "source-a",
                "target_drone_id": "target-a",
                "system": "snes",
                "rom_name": "Game.zip",
                "relative_path": "Game.zip",
                "action": "download",
                "status": "completed",
                "bytes_transferred": 8,
                "file_size": 8,
                "rom_md5": "abc",
                "download_started_at": "2026-01-01T00:00:00+00:00",
                "download_completed_at": "2026-01-01T00:00:01+00:00",
                "duration_ms": 1000,
            }
            def fake_download(*args, **kwargs):
                progress = kwargs.get("progress_callback")
                if progress:
                    progress(4, 8)
                    progress(8, 8)
                return dict(completed)

            with mock.patch("app.drone_api.DOWNLOAD_PROGRESS_PUSH_SECONDS", 0), mock.patch(
                "app.drone_api._download_rom_from_peer", side_effect=fake_download
            ), mock.patch.object(repo, "list_assets", return_value=(root / "roms" / "snes", [])), mock.patch(
                "app.drone_api._post_download_state"
            ) as post_download_state, mock.patch("app.drone_api._post_rom_sync_activity") as post_activity:
                manager._run_job(queued["job_id"])

            push_reasons = [call.kwargs.get("reason") for call in post_download_state.call_args_list]
            self.assertEqual(push_reasons[0], "started")
            self.assertIn("progress", push_reasons)
            self.assertEqual(push_reasons[-1], "completed")
            post_activity.assert_called_once()
            pushed = post_activity.call_args.args[2]
            self.assertEqual(pushed["status"], "completed")
            self.assertEqual(pushed["sync_id"], queued["job_id"])
            self.assertEqual(pushed["rom_md5"], "abc")

    def test_best_peer_for_rom_respects_source_device_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "OVERMIND_DEVICE_ID": "target-a",
                },
                clear=True,
            ):
                settings = Settings.from_env()
            swarm_path = root / "system" / "drone-app" / "overmind_swarm.json"
            swarm_path.parent.mkdir(parents=True)
            swarm_path.write_text(
                json.dumps(
                    [
                        {
                            "device_id": "source-without-rom",
                            "online": True,
                            "public_resolvable": True,
                            "public_reachable_url": "https://198.51.100.19:8443",
                            "rom_systems": ["snes"],
                            "last_speed_sample": {"upload_mbps": 500},
                        },
                        {
                            "device_id": "source-with-rom",
                            "online": True,
                            "public_resolvable": True,
                            "public_reachable_url": "https://198.51.100.20:8443",
                            "rom_systems": ["snes"],
                            "last_speed_sample": {"upload_mbps": 10},
                        },
                    ]
                ),
                encoding="utf-8",
            )
            peer = _best_peer_for_rom(
                settings,
                RomRepository(settings.roms_root, settings.bios_root),
                {},
                "snes",
                "Game.zip",
                source_device_ids={"source-with-rom"},
            )
            self.assertIsNotNone(peer)
            self.assertEqual(peer["device_id"], "source-with-rom")

    def test_best_peer_for_rom_rejects_unresolvable_source_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "OVERMIND_DEVICE_ID": "target-a",
                },
                clear=True,
            ):
                settings = Settings.from_env()
            swarm_path = root / "system" / "drone-app" / "overmind_swarm.json"
            swarm_path.parent.mkdir(parents=True)
            swarm_path.write_text(
                json.dumps([{
                    "device_id": "unreachable-source",
                    "online": True,
                    "public_resolvable": False,
                    "rom_systems": ["snes"],
                    "last_speed_sample": {"upload_mbps": 500},
                }]),
                encoding="utf-8",
            )

            peer = _best_peer_for_rom(
                settings,
                RomRepository(settings.roms_root, settings.bios_root),
                {},
                "snes",
                "Game.zip",
                source_device_ids={"unreachable-source"},
            )

            self.assertIsNone(peer)

    def test_disk_rom_without_gamelist_is_listed_with_md5(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rom = root / "roms" / "snes" / "Loose Game (USA).zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"loose-rom")
            repo = RomRepository(root / "roms", root / "bios")

            _, roms = repo.list_assets("snes", "roms")

            self.assertEqual(len(roms), 1)
            self.assertEqual(roms[0]["rom_path"], "Loose Game (USA).zip")
            self.assertEqual(roms[0]["source"], "disk")
            self.assertFalse(roms[0]["has_gamelist_entry"])
            self.assertEqual(roms[0]["md5"], RomRepository.build_md5(rom))

    def test_rom_list_can_skip_md5_for_fast_ui_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rom = root / "roms" / "snes" / "Loose Game (USA).zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"loose-rom")
            repo = RomRepository(root / "roms", root / "bios")

            with mock.patch.object(RomRepository, "build_md5", side_effect=AssertionError("should not hash")):
                _, roms = repo.list_assets("snes", "roms", include_md5=False)

            self.assertEqual(len(roms), 1)
            self.assertNotIn("md5", roms[0])
            self.assertNotIn("rom_md5", roms[0])
            self.assertEqual(roms[0]["rom_path"], "Loose Game (USA).zip")

    def test_ps3_folder_rom_is_listed_without_md5(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / "roms" / "ps3" / "Demon Souls.ps3"
            (game / "PS3_GAME" / "USRDIR").mkdir(parents=True)
            (game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").write_bytes(b"boot")
            (game / "PS3_DISC.SFB").write_bytes(b"disc")
            repo = RomRepository(root / "roms", root / "bios")

            with mock.patch.object(RomRepository, "build_md5", side_effect=AssertionError("folder ROM should not hash")):
                _, roms = repo.list_assets("ps3", "roms")

            self.assertEqual(len(roms), 1)
            self.assertEqual(roms[0]["entry_type"], "folder")
            self.assertFalse(roms[0]["is_downloadable"])
            self.assertEqual(roms[0]["file_path"], "Demon Souls.ps3")
            self.assertEqual(roms[0]["file_size"], 8)
            self.assertNotIn("md5", roms[0])
            self.assertNotIn("rom_md5", roms[0])

    def test_gamelist_metadata_enriches_matching_disk_rom_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Chrono Trigger (USA).zip").write_bytes(b"chrono")
            (system / "gamelist.xml").write_text(
                """<gameList>
  <game><path>./Chrono Trigger (USA).zip</path><name>Chrono Trigger Deluxe</name></game>
  <game><path>./Missing Game.zip</path><name>Missing Game</name></game>
</gameList>""",
                encoding="utf-8",
            )
            repo = RomRepository(root / "roms", root / "bios")

            _, roms = repo.list_assets("snes", "roms")

            self.assertEqual([rom["title"] for rom in roms], ["Chrono Trigger Deluxe"])
            self.assertEqual(roms[0]["metadata_source"], "gamelist.xml")

    def test_md5_identity_and_collision_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            existing = system / "Asteroids (USA).zip"
            existing.write_bytes(b"one")
            (system / "Renamed Asteroids.zip").write_bytes(b"one")
            repo = RomRepository(root / "roms", root / "bios")

            self.assertTrue(_rom_md5_exists(repo, RomRepository.build_md5(existing)))
            self.assertEqual(_collision_safe_target(system, "Asteroids (USA).zip").name, "Asteroids (USA) (2).zip")

    def test_legacy_shutdown_action_is_rejected_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()
            repo = RomRepository(root / "roms", root / "bios")
            with mock.patch("app.drone_api.subprocess.Popen") as popen:
                status, message, result = _execute_overmind_action(settings, repo, {"action": "shutdown"})
            self.assertEqual(status, "failed")
            self.assertIn("disabled", message)
            self.assertIsNone(result)
            popen.assert_not_called()

    def test_kiosk_actions_update_es_settings_and_restart_emulationstation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            es_settings = root / "system" / "configs" / "emulationstation" / "es_settings.cfg"
            es_settings.parent.mkdir(parents=True)
            es_settings.write_text('<string name="ThemeSet" value="carbon" />\n', encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ES_SETTINGS_FILE": str(es_settings)},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(root / "roms", root / "bios")

            with mock.patch("app.drone_api.shutil.which", return_value="/usr/bin/batocera-es-swissknife"), mock.patch(
                "app.drone_api.subprocess.Popen"
            ) as popen:
                enabled_status, enabled_message, enabled_result = _execute_overmind_action(
                    settings, repo, {"action": "enable_kiosk"}
                )
                self.assertIn('name="UIMode" value="Kiosk"', es_settings.read_text(encoding="utf-8"))
                disabled_status, disabled_message, disabled_result = _execute_overmind_action(
                    settings, repo, {"action": "disable_kiosk"}
                )

            self.assertEqual(enabled_status, "completed")
            self.assertIn("Kiosk mode enabled", enabled_message)
            self.assertTrue(enabled_result["enabled"])
            self.assertEqual(disabled_status, "completed")
            self.assertIn("Kiosk mode disabled", disabled_message)
            self.assertFalse(disabled_result["enabled"])
            self.assertNotIn('name="UIMode"', es_settings.read_text(encoding="utf-8"))
            self.assertEqual(popen.call_count, 2)
            popen.assert_any_call(
                ["/usr/bin/batocera-es-swissknife", "--restart"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def test_reclaim_overmind_token_after_heartbeat_unauthorized_uses_bound_auth_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "bff-drone-a"}, clear=True):
                settings = Settings.from_env()
            config = {
                "overmind_url": "https://bff-overmind:8000",
                "overmind_auth_token": "onboarding-token",
                "overmind_token": "stale-drone-token",
            }
            error = HTTPError(
                "https://bff-overmind:8000/api/devices/bff-drone-a/heartbeat",
                401,
                "Unauthorized",
                {},
                io.BytesIO(b'{"detail":"Invalid Drone token"}'),
            )
            with mock.patch("app.drone_api._register_or_claim_overmind_token", return_value="onboarding-token") as register:
                token = _reclaim_overmind_token_after_unauthorized(
                    settings,
                    RomRepository(root / "roms", root / "bios"),
                    config,
                    "https://bff-overmind:8000",
                    error,
                )

            self.assertEqual(token, "onboarding-token")
            self.assertNotIn("overmind_token", config)
            self.assertEqual(config["integration_state"], "credential_reclaim")
            register.assert_called_once()

    def test_pending_overmind_approval_keeps_integration_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "bff-drone-a"}, clear=True):
                settings = Settings.from_env()
            config = {
                "overmind_url": "https://bff-overmind:8000",
                "overmind_email": "overlord@example.com",
                "overmind_auth_token": "onboarding-token",
                "integration_enabled": True,
            }
            with mock.patch(
                "app.drone_api._overmind_post_json",
                return_value={
                    "status": "pending",
                    "message": "Psionic connection detected. Awaiting Overlord approval.",
                },
            ):
                token = _register_or_claim_overmind_token(
                    settings,
                    RomRepository(root / "roms", root / "bios"),
                    config,
                    "https://bff-overmind:8000",
                )

            self.assertIsNone(token)
            saved = _load_overmind_config_for_settings(settings)
            self.assertTrue(saved.get("integration_enabled"))
            self.assertEqual(saved.get("integration_state"), "pending_approval")
            self.assertEqual(saved.get("overmind_auth_token"), "onboarding-token")

    def test_rejected_overmind_authorization_token_clears_connected_swarm_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "bff-drone-b"}, clear=True):
                settings = Settings.from_env()
            config = {
                "overmind_url": "https://bff-overmind:8000",
                "overmind_auth_token": "shared-token",
                "overmind_token": "old-drone-token",
                "integration_enabled": True,
                "integration_state": "polling",
                "swarm_connection_status": "connected",
            }
            swarm_path = root / "system" / "drone-app" / "overmind_swarm.json"
            swarm_path.parent.mkdir(parents=True)
            swarm_path.write_text(json.dumps([{"device_id": "peer-a"}]), encoding="utf-8")

            error = HTTPError(
                "https://bff-overmind:8000/api/devices/register",
                401,
                "Unauthorized",
                hdrs=None,
                fp=None,
            )
            error.url = "https://bff-overmind:8000/api/devices/register"
            with mock.patch("app.drone_api._overmind_post_json", side_effect=error):
                token = _register_or_claim_overmind_token(
                    settings,
                    RomRepository(root / "roms", root / "bios"),
                    config,
                    "https://bff-overmind:8000",
                )

            self.assertIsNone(token)
            saved = _load_overmind_config_for_settings(settings)
            self.assertFalse(saved.get("overmind_token"))
            self.assertFalse(saved.get("integration_enabled"))
            self.assertEqual(saved.get("integration_state"), "pending_failed")
            self.assertEqual(saved.get("swarm_connection_status"), "disconnected")
            self.assertIn("HTTPError status=401", saved.get("last_error") or "")
            self.assertEqual(json.loads(swarm_path.read_text(encoding="utf-8")), [])

    def test_gpu_info_tolerates_unavailable_detection(self) -> None:
        with mock.patch("app.drone_api.subprocess.run", side_effect=FileNotFoundError()):
            info = _collect_gpu_info()
        self.assertIn("vendor", info)
        self.assertIn("pci_devices", info)

    def test_fake_overmind_config_is_ignored_when_fake_data_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            config_path = root / "system" / "drone-app" / "overmind_integration.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                '{"overmind_url":"https://overmind.local:9443","overmind_email":"%s","overmind_token":"%s","integration_enabled":true}'
                % (FAKE_OVERMIND_EMAIL, FAKE_OVERMIND_TOKEN),
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "USE_FAKE_DATA": "false"},
                clear=True,
            ):
                settings = Settings.from_env()

            loaded = _load_overmind_config_for_settings(settings)
            self.assertEqual(loaded.get("overmind_email"), "")
            self.assertFalse(loaded.get("overmind_token"))
            self.assertFalse(loaded.get("integration_enabled"))

    def test_seeded_mock_userdata_is_not_used_as_real_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "USE_FAKE_DATA": "false",
                },
                clear=True,
            ):
                settings = Settings.from_env()

            roms_root, bios_root = _real_data_roots(settings)
            self.assertNotEqual(roms_root, root / "roms")
            self.assertNotEqual(bios_root, root / "bios")
            self.assertFalse(RomRepository(roms_root, bios_root).search_roms("mario"))

    def test_seeded_mock_userdata_with_real_roms_keeps_real_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            (root / "roms" / "dreamcast").mkdir(parents=True)
            (root / "roms" / "dreamcast" / "Real Game.chd").write_bytes(b"REAL-ROM")
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "USE_FAKE_DATA": "false",
                },
                clear=True,
            ):
                settings = Settings.from_env()

            roms_root, bios_root = _real_data_roots(settings)
            self.assertEqual(roms_root, root / "roms")
            self.assertEqual(bios_root, root / "bios")

    def test_collect_rom_metadata_tolerates_missing_rom_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "missing-roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "USE_FAKE_DATA": "false",
                },
                clear=True,
            ):
                settings = Settings.from_env()

            result = _collect_rom_metadata(settings, RomRepository(settings.roms_root, settings.bios_root))
            self.assertEqual(result["systems"], [])
            self.assertEqual(result["roms"], [])

    def test_collect_rom_metadata_includes_bios_md5(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            bios = root / "bios" / "dc" / "flash.bin"
            bios.parent.mkdir(parents=True)
            bios.write_bytes(b"bios-data")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()

            result = _collect_rom_metadata(settings, RomRepository(settings.roms_root, settings.bios_root))
            self.assertEqual(len(result["bios"]), 1)
            self.assertEqual(result["bios"][0]["path"], "dc/flash.bin")
            self.assertTrue(result["bios"][0]["md5"])

    def test_collect_asset_metadata_includes_artwork_types_from_gamelist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Game.zip").write_bytes(b"rom-data")
            (system / "gamelist.xml").write_text(
                "<gameList><game><path>./Game.zip</path><name>Game</name>"
                "<image>./images/game.png</image><marquee>./images/game-marquee.png</marquee>"
                "</game></gameList>\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()

            result = _collect_rom_metadata(settings, RomRepository(settings.roms_root, settings.bios_root))

            self.assertEqual(len(result["artwork"]), 1)
            self.assertEqual(result["artwork"][0]["asset_type"], "artwork")
            self.assertEqual(result["artwork"][0]["system"], "snes")
            self.assertEqual(result["artwork"][0]["rom_path"], "Game.zip")
            self.assertEqual(result["artwork"][0]["artwork_types"], ["image", "marquee"])
            self.assertNotIn("artwork_paths", result["artwork"][0])

    def test_rom_metadata_cache_reuses_md5_and_detects_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"first")
            bios = root / "bios" / "dc" / "flash.bin"
            bios.parent.mkdir(parents=True)
            bios.write_bytes(b"bios-data")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)

            snapshot, changed, stats = _poll_rom_metadata_cache(settings, repo)
            self.assertTrue(changed)
            self.assertEqual(stats["new_or_changed"], 1)
            self.assertEqual(stats["bios_new_or_changed"], 1)
            self.assertNotIn("rom_md5", snapshot["roms"][0])
            first_bios_md5 = snapshot["bios"][0]["bios_md5"]
            patches = list(_hash_rom_metadata_batches(settings, repo, batch_size=1))
            self.assertEqual(len(patches), 1)
            first_md5 = patches[0]["roms"][0]["rom_md5"]
            cache = _rom_metadata_cache_path(settings)
            cache_data = json.loads(cache.read_text(encoding="utf-8"))
            cache_data["dirty"] = False
            cache.write_text(json.dumps(cache_data), encoding="utf-8")

            with mock.patch.object(RomRepository, "build_md5", side_effect=AssertionError("unchanged metadata should not hash")):
                snapshot, changed, stats = _poll_rom_metadata_cache(settings, repo)
            self.assertFalse(changed)
            self.assertEqual(snapshot["roms"][0]["rom_md5"], first_md5)
            self.assertEqual(snapshot["bios"][0]["bios_md5"], first_bios_md5)

            rom.unlink()
            bios.unlink()
            snapshot, changed, stats = _poll_rom_metadata_cache(settings, repo)
            self.assertTrue(changed)
            self.assertEqual(stats["deleted"], 1)
            self.assertEqual(stats["bios_deleted"], 1)
            self.assertEqual(snapshot["roms"], [])
            self.assertEqual(snapshot["bios"], [])

    def test_asset_metadata_cache_detects_artwork_updates_without_rehashing_roms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Game.zip").write_bytes(b"rom-data")
            gamelist = system / "gamelist.xml"
            gamelist.write_text(
                "<gameList><game><path>./Game.zip</path><name>Game</name>"
                "<image>./images/game.png</image>"
                "</game></gameList>\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)
            _poll_rom_metadata_cache(settings, repo)
            list(_hash_rom_metadata_batches(settings, repo, batch_size=1))
            cache_data = json.loads(_rom_metadata_cache_path(settings).read_text(encoding="utf-8"))
            cache_data["dirty"] = False
            _rom_metadata_cache_path(settings).write_text(json.dumps(cache_data), encoding="utf-8")
            gamelist.write_text(
                "<gameList><game><path>./Game.zip</path><name>Game</name>"
                "<image>./images/game.png</image><marquee>./images/game-marquee.png</marquee>"
                "</game></gameList>\n",
                encoding="utf-8",
            )

            with mock.patch.object(RomRepository, "build_md5", side_effect=AssertionError("unchanged ROM should not hash")):
                snapshot, changed, stats = _poll_rom_metadata_cache(settings, repo)

            self.assertTrue(changed)
            self.assertTrue(stats["artwork_changed"])
            self.assertEqual(snapshot["artwork"][0]["artwork_types"], ["image", "marquee"])

    def test_corrupt_rom_metadata_cache_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"first")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            _rom_metadata_cache_path(settings).parent.mkdir(parents=True)
            _rom_metadata_cache_path(settings).write_text("{broken", encoding="utf-8")

            snapshot, changed, stats = _poll_rom_metadata_cache(settings, RomRepository(settings.roms_root, settings.bios_root))
            self.assertTrue(changed)
            self.assertTrue(stats["rebuilt"])
            self.assertEqual(len(snapshot["roms"]), 1)

    def test_rom_metadata_scan_checkpoint_survives_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "First.zip").write_bytes(b"first")
            (system / "Second.zip").write_bytes(b"second")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)
            interrupted = False

            def interrupt_after_checkpoint(path, payload):
                nonlocal interrupted
                _write_json_file(path, payload)
                if payload.get("scan_in_progress") and not interrupted:
                    interrupted = True
                    raise RuntimeError("simulated reset")

            with mock.patch("app.drone_api.ROM_METADATA_PROGRESS_FILES", 1), mock.patch(
                "app.drone_api._write_json_file", side_effect=interrupt_after_checkpoint
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated reset"):
                    _poll_rom_metadata_cache(settings, repo)

            partial = json.loads(_rom_metadata_cache_path(settings).read_text(encoding="utf-8"))
            self.assertTrue(partial["scan_in_progress"])
            self.assertEqual(len(partial["entries"]), 1)

            snapshot, changed, _ = _poll_rom_metadata_cache(settings, repo)
            self.assertTrue(changed)
            self.assertEqual(len(snapshot["roms"]), 2)

    def test_bios_hash_checkpoint_resumes_without_rehashing_completed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            bios = root / "bios"
            bios.mkdir(parents=True)
            (bios / "A.bin").write_bytes(b"a")
            (bios / "B.bin").write_bytes(b"b")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(bios)},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)
            original_md5 = RomRepository.build_md5

            def interrupted_hash(path):
                if path.name == "B.bin":
                    raise RuntimeError("simulated reset")
                return original_md5(path)

            with mock.patch("app.drone_api.ROM_METADATA_PROGRESS_FILES", 1), mock.patch.object(
                RomRepository, "build_md5", side_effect=interrupted_hash
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated reset"):
                    _poll_rom_metadata_cache(settings, repo)

            partial = json.loads(_rom_metadata_cache_path(settings).read_text(encoding="utf-8"))
            self.assertTrue(partial["bios_entries"]["a.bin"]["bios_md5"])
            self.assertNotIn("bios_md5", partial["bios_entries"]["b.bin"])

            hashed_after_restart = []

            def track_hash(path):
                hashed_after_restart.append(path.name)
                return original_md5(path)

            with mock.patch.object(RomRepository, "build_md5", side_effect=track_hash):
                snapshot, _, _ = _poll_rom_metadata_cache(settings, repo)

            self.assertEqual(hashed_after_restart, ["B.bin"])
            self.assertEqual(len(snapshot["bios"]), 2)

    def test_rom_hash_checkpoint_resumes_inside_large_upload_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "A.zip").write_bytes(b"a")
            (system / "B.zip").write_bytes(b"b")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)
            _poll_rom_metadata_cache(settings, repo)
            original_md5 = RomRepository.build_md5

            def interrupted_hash(path):
                if path.name == "B.zip":
                    raise RuntimeError("simulated reset")
                return original_md5(path)

            with mock.patch("app.drone_api.ROM_METADATA_PROGRESS_FILES", 1), mock.patch.object(
                repo, "build_md5", side_effect=interrupted_hash
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated reset"):
                    list(_hash_rom_metadata_batches(settings, repo, batch_size=1000))

            partial = json.loads(_rom_metadata_cache_path(settings).read_text(encoding="utf-8"))
            self.assertTrue(partial["entries"]["snes:A.zip"]["rom_md5"])
            self.assertNotIn("rom_md5", partial["entries"]["snes:B.zip"])

            hashed_after_restart = []

            def track_hash(path):
                hashed_after_restart.append(path.name)
                return original_md5(path)

            with mock.patch.object(repo, "build_md5", side_effect=track_hash):
                patches = list(_hash_rom_metadata_batches(settings, repo, batch_size=1000))

            self.assertEqual(hashed_after_restart, ["B.zip"])
            self.assertEqual(len(patches), 1)
            self.assertEqual(patches[0]["roms"][0]["file_path"], "B.zip")

    def test_rom_metadata_sync_skips_unchanged_cache_without_rehashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"first")
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "OVERMIND_DEVICE_ID": "drone-a",
                },
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)
            _poll_rom_metadata_cache(settings, repo)
            list(_hash_rom_metadata_batches(settings, repo, batch_size=1))
            cache_data = json.loads(_rom_metadata_cache_path(settings).read_text(encoding="utf-8"))
            cache_data["dirty"] = False
            _rom_metadata_cache_path(settings).write_text(json.dumps(cache_data), encoding="utf-8")

            uploads = []

            def fake_post(url, payload, token=None, settings=None):
                uploads.append((url, payload, token))
                return 200, {
                    "rom_count": len(payload.get("roms") or []),
                    "bios_count": len(payload.get("bios") or []),
                    "artwork_count": len(payload.get("artwork") or []),
                }

            with mock.patch.object(RomRepository, "build_md5", side_effect=AssertionError("unchanged ROM should not hash")), mock.patch(
                "app.drone_api._overmind_post_json_with_status", side_effect=fake_post
            ):
                result = _sync_rom_metadata_to_overmind(
                    settings,
                    repo,
                    {"overmind_token": "drone-token"},
                    "https://overmind.local",
                    "drone-token",
                )

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "no_changes")
            self.assertFalse(result["changed"])
            self.assertEqual(result["rom_count"], 1)
            self.assertEqual(result["bios_count"], 0)
            self.assertEqual(result["artwork_count"], 0)
            self.assertEqual(len(uploads), 0)
            cache_after = json.loads(_rom_metadata_cache_path(settings).read_text(encoding="utf-8"))
            self.assertFalse(cache_after["dirty"])
            self.assertFalse(cache_after["last_successful_upload_at"])

    def test_rom_metadata_sync_uploads_inventory_then_batched_md5_patches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Game One.zip").write_bytes(b"one")
            (system / "Game Two.zip").write_bytes(b"two")
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "OVERMIND_DEVICE_ID": "drone-a",
                },
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)
            uploads = []

            def fake_post(url, payload, token=None, settings=None):
                uploads.append(payload)
                return 200, {
                    "rom_count": len(payload.get("roms") or []),
                    "bios_count": len(payload.get("bios") or []),
                    "artwork_count": len(payload.get("artwork") or []),
                }

            with mock.patch("app.drone_api.ROM_METADATA_MD5_BATCH_SIZE", 1), mock.patch(
                "app.drone_api._overmind_post_json_with_status", side_effect=fake_post
            ):
                result = _sync_rom_metadata_to_overmind(
                    settings,
                    repo,
                    {"overmind_token": "drone-token"},
                    "https://overmind.local",
                    "drone-token",
                )

            self.assertEqual(result["hash_batches"], 2)
            self.assertEqual(result["hashed_roms"], 2)
            self.assertEqual([payload["update_mode"] for payload in uploads], ["inventory", "rom_hash_patch", "rom_hash_patch"])
            self.assertEqual(len(uploads[0]["roms"]), 2)
            self.assertTrue(all("rom_md5" not in row for row in uploads[0]["roms"]))
            self.assertTrue(all(len(payload["roms"]) == 1 and payload["roms"][0].get("rom_md5") for payload in uploads[1:]))

    def test_rom_metadata_sync_persists_and_uploads_added_and_deleted_roms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            first = system / "First Game.zip"
            second = system / "Second Game.zip"
            first.write_bytes(b"one")
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "OVERMIND_DEVICE_ID": "drone-a",
                },
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)
            uploads = []

            def fake_post(url, payload, token=None, settings=None):
                uploads.append(payload)
                return 200, {
                    "rom_count": len(payload.get("roms") or []),
                    "bios_count": len(payload.get("bios") or []),
                    "artwork_count": len(payload.get("artwork") or []),
                }

            with mock.patch("app.drone_api._overmind_post_json_with_status", side_effect=fake_post):
                _sync_rom_metadata_to_overmind(settings, repo, {}, "https://overmind.local", "drone-token")
                second.write_bytes(b"two")
                added = _sync_rom_metadata_to_overmind(settings, repo, {}, "https://overmind.local", "drone-token")
                first.unlink()
                deleted = _sync_rom_metadata_to_overmind(settings, repo, {}, "https://overmind.local", "drone-token")

            inventories = [payload for payload in uploads if payload.get("update_mode") == "inventory"]
            self.assertEqual([len(payload["roms"]) for payload in inventories], [1, 2, 1])
            self.assertEqual(added["stats"]["new_or_changed"], 1)
            self.assertEqual(deleted["stats"]["deleted"], 1)
            cache = json.loads(_rom_metadata_cache_path(settings).read_text(encoding="utf-8"))
            self.assertEqual(len(cache["entries"]), 1)
            self.assertEqual(next(iter(cache["entries"].values()))["file_path"], "Second Game.zip")
            self.assertFalse(cache["dirty"])

    def test_rom_metadata_poll_caches_and_hashes_without_overmind_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"offline-rom")
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "OVERMIND_URL": "",
                    "OVERMIND_DEVICE_ID": "drone-a",
                },
                clear=True,
            ):
                settings = Settings.from_env()

            result = _poll_rom_metadata_once(settings, RomRepository(settings.roms_root, settings.bios_root))

            self.assertEqual(result["status"], "cached")
            self.assertEqual(result["reason"], "overmind_not_configured")
            self.assertEqual(result["hashed_roms"], 1)
            cache = json.loads(_rom_metadata_cache_path(settings).read_text(encoding="utf-8"))
            self.assertTrue(cache["dirty"])
            self.assertTrue(next(iter(cache["entries"].values()))["rom_md5"])

    def test_rom_metadata_poll_finishes_local_cache_when_overmind_upload_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"offline-rom")
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "OVERMIND_URL": "https://overmind.local",
                    "OVERMIND_DRONE_TOKEN": "drone-token",
                    "OVERMIND_DEVICE_ID": "drone-a",
                },
                clear=True,
            ):
                settings = Settings.from_env()

            with mock.patch(
                "app.drone_api._overmind_post_json_with_status",
                side_effect=URLError("offline"),
            ):
                with self.assertRaises(URLError):
                    _poll_rom_metadata_once(settings, RomRepository(settings.roms_root, settings.bios_root))

            cache = json.loads(_rom_metadata_cache_path(settings).read_text(encoding="utf-8"))
            self.assertTrue(cache["dirty"])
            self.assertTrue(next(iter(cache["entries"].values()))["rom_md5"])

    def test_sample_speed_uses_cloudflare_speed_test_endpoints(self) -> None:
            calls = []

            def fake_raw_request(url, data=None):
                calls.append((url, data))
                if data is None:
                    return b"0" * 1000000 if "bytes=1000000" in url else b""
                return b""

            with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
                "app.drone_api._speed_test_raw_request", side_effect=fake_raw_request
            ):
                sample = _sample_speed()

            self.assertEqual(sample["source"], "cloudflare-speed-test")
            self.assertGreater(sample["download_mbps"], 0)
            self.assertGreater(sample["upload_mbps"], 0)
            self.assertEqual(
                [call[0] for call in calls],
                [
                    "https://speed.cloudflare.com/__down?bytes=0",
                    "https://speed.cloudflare.com/__down?bytes=1000000",
                    "https://speed.cloudflare.com/__up",
                ],
            )
            self.assertEqual(len(calls[2][1]), 1000000)

    def test_sample_speed_allows_configured_speed_test_service_and_reports_failure(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"DRONE_SPEED_TEST_BASE_URL": "https://speed.example.test/", "DRONE_SPEED_TEST_BYTES": "4096"},
            clear=True,
        ), mock.patch("app.drone_api._speed_test_raw_request", side_effect=URLError("offline")):
            sample = _sample_speed()

        self.assertEqual(sample["source"], "external-speed-test-failed")
        self.assertEqual(sample["bytes"], 4096)
        self.assertIn("URLError", sample["error"])


class RepositoryTests(unittest.TestCase):
    def test_list_systems_from_mock_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            (root / "roms" / "snes.old").mkdir(parents=True)
            (root / "roms" / "snes.old" / "Old Game.zip").write_bytes(b"old")
            (root / "roms" / "snes.old" / "gamelist.xml").write_text(
                "<gameList><game><path>./Old Game.zip</path><name>Old Game</name></game></gameList>\n",
                encoding="utf-8",
            )
            repo = RomRepository(root / "roms", root / "bios")
            systems = repo.list_systems()
            names = {item["name"] for item in systems}
            self.assertIn("snes", names)
            self.assertIn("gba", names)
            self.assertNotIn("snes.old", names)

    def test_list_systems_does_not_hash_rom_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Large Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"rom")
            repo = RomRepository(root / "roms", root / "bios")

            with mock.patch.object(RomRepository, "build_md5", side_effect=AssertionError("should not hash")):
                systems = repo.list_systems()

            self.assertEqual(systems, [{"name": "snes", "rom_count": 1}])

    def test_search_roms_from_mock_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            repo = RomRepository(root / "roms", root / "bios")
            results = repo.search_roms("mario")
            self.assertTrue(any(item["name"].lower().startswith("mario") for item in results))

    def test_list_missing_artwork_from_gamelist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            repo = RomRepository(root / "roms", root / "bios")
            results = repo.list_missing_artwork()
            chrono = next(item for item in results if item["system"] == "snes" and "Chrono" in item["name"])
            self.assertIn("image", chrono["missing"])
            self.assertIn("marquee", chrono["missing"])
            self.assertEqual(chrono["rom_name"], "Chrono Trigger (USA).zip")

    def test_apply_launchbox_artwork_only_missing_fields(self) -> None:
        class FakeLaunchBoxClient:
            def details(self, game_key: str) -> dict:
                return {
                    "game_key": game_key,
                    "name": "Chrono Trigger",
                    "platform": "Super Nintendo Entertainment System",
                    "images": [
                        {"url": "https://example.test/front.jpg", "file_name": "front.jpg", "type": "Box - Front"},
                        {"url": "https://example.test/logo.png", "file_name": "logo.png", "type": "Clear Logo"},
                        {"url": "https://example.test/fanart.jpg", "file_name": "fanart.jpg", "type": "Fanart - Background"},
                    ],
                }

            def choose_image_for_field(self, details: dict, field: str) -> dict:
                for image in details["images"]:
                    if field == "marquee" and image["type"] == "Clear Logo":
                        return image
                    if field != "marquee" and image["type"] == "Box - Front":
                        return image
                return details["images"][0]

            def download_image(self, url: str):
                return b"image-bytes", "image/jpeg"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            gamelist = root / "roms" / "snes" / "gamelist.xml"
            gamelist.write_text(
                "<gameList><game><path>./Chrono Trigger (USA).zip</path><name>Chrono Trigger</name><image>./images/existing.png</image></game></gameList>\n",
                encoding="utf-8",
            )
            repo = RomRepository(root / "roms", root / "bios")
            rom = next(item for item in repo.search_roms("chrono") if item["system"] == "snes")
            result = repo.apply_launchbox_artwork("snes", rom["unique_id"], "123", FakeLaunchBoxClient())
            updated_fields = {item["field"] for item in result["updated"]}
            self.assertNotIn("image", updated_fields)
            self.assertIn("thumbnail", updated_fields)
            self.assertIn("marquee", updated_fields)
            text = gamelist.read_text(encoding="utf-8")
            self.assertIn("./images/existing.png", text)
            self.assertIn("launchbox-marquee", text)

    def test_apply_launchbox_artwork_imports_missing_metadata(self) -> None:
        class FakeLaunchBoxClient:
            def details(self, game_key: str) -> dict:
                return {
                    "game_key": game_key,
                    "name": "Chrono Trigger",
                    "platform": "Super Nintendo Entertainment System",
                    "overview": "A time travel RPG.",
                    "release_date": "1995-08-22",
                    "genre": "Role-Playing",
                    "developer": "Square",
                    "publisher": "Square",
                    "images": [],
                }

            def choose_image_for_field(self, details: dict, field: str):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            gamelist = root / "roms" / "snes" / "gamelist.xml"
            gamelist.write_text(
                "<gameList><game><path>./Chrono Trigger (USA).zip</path><name>Existing Name</name></game></gameList>\n",
                encoding="utf-8",
            )
            repo = RomRepository(root / "roms", root / "bios")
            rom = next(item for item in repo.search_roms("chrono") if item["system"] == "snes")
            result = repo.apply_launchbox_artwork(
                "snes",
                rom["unique_id"],
                "123",
                FakeLaunchBoxClient(),
                import_metadata=True,
            )
            metadata_fields = {item["field"] for item in result["updated"] if item.get("source") == "launchbox_metadata"}
            self.assertNotIn("name", metadata_fields)
            self.assertIn("desc", metadata_fields)
            self.assertIn("genre", metadata_fields)
            text = gamelist.read_text(encoding="utf-8")
            self.assertIn("<name>Existing Name</name>", text)
            self.assertIn("<desc>A time travel RPG.</desc>", text)

    def test_remove_gamelist_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            repo = RomRepository(root / "roms", root / "bios")
            result = repo.remove_gamelist_entry("snes", "Chrono Trigger (USA).zip")
            self.assertTrue(result["removed"])
            text = (root / "roms" / "snes" / "gamelist.xml").read_text(encoding="utf-8")
            self.assertNotIn("Chrono Trigger", text)

    def test_remove_gamelist_entries_reports_write_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            repo = RomRepository(root / "roms", root / "bios")
            with mock.patch("xml.etree.ElementTree.ElementTree.write", side_effect=PermissionError("Operation not permitted")):
                result = repo.remove_gamelist_entries([{"system": "snes", "rom_path": "Chrono Trigger (USA).zip"}])

            self.assertEqual(result["removed_count"], 0)
            self.assertEqual(result["failed_count"], 1)
            self.assertIn("Operation not permitted", result["failed"][0]["error"])
            text = (root / "roms" / "snes" / "gamelist.xml").read_text(encoding="utf-8")
            self.assertIn("Chrono Trigger", text)

    def test_update_gamelist_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            repo = RomRepository(root / "roms", root / "bios")
            result = repo.update_gamelist_entry(
                "snes",
                "Chrono Trigger (USA).zip",
                {"name": "Chrono Trigger Updated", "desc": "A time travel RPG.", "genre": ""},
            )

            self.assertEqual(result["title"], "Chrono Trigger Updated")
            self.assertEqual(result["gamelist"]["desc"], "A time travel RPG.")
            text = (root / "roms" / "snes" / "gamelist.xml").read_text(encoding="utf-8")
            self.assertIn("Chrono Trigger Updated", text)
            self.assertIn("A time travel RPG.", text)
            self.assertNotIn("<genre>", text)


class LaunchBoxMappingTests(unittest.TestCase):
    def test_launchbox_title_cleanup_replaces_special_separators(self) -> None:
        self.assertEqual(_clean_rom_title("Mega Man: The-Wily;Wars [USA] <Rev 1>.zip"), "Mega Man The Wily Wars USA Rev 1")

    def test_batocera_system_maps_to_launchbox_platform_name(self) -> None:
        self.assertEqual(_launchbox_platform_for_system("ps2"), "Sony Playstation 2")
        self.assertEqual(_launchbox_platform_for_system("snes"), "Super Nintendo Entertainment System")

    def test_launchbox_search_supplies_platform_filter(self) -> None:
        urls = []

        class FakeLaunchBoxClient(LaunchBoxClient):
            def _get_json(self, url: str) -> dict:
                urls.append(url)
                return {"data": []}

        FakeLaunchBoxClient().search("Chrono Trigger", system="ps2")
        self.assertTrue(urls)
        self.assertIn("platform=Sony%20Playstation%202", urls[0])


if __name__ == "__main__":
    unittest.main()
