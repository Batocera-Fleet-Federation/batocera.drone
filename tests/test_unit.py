import base64
import hashlib
import importlib
import io
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from threading import Event
from unittest import mock
from pathlib import Path
from urllib.error import URLError

import app.drone_api as drone_api
from app.mock_data import seed_mock_userdata
from app.state_store import database_path, database_path_for_legacy_file, load_payload, open_database, save_payload
from app.overmind_reporting import (
    list_emulator_config_files,
    read_emulator_config_file,
)
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
    _drone_network_payload,
    _peer_address,
    _peer_api_port,
    _peer_health_url,
    _probe_peer_public_ip,
    _get_local_ip_addresses,
    _get_router_ip_address,
    _collect_gpu_info,
    _format_overmind_error,
    _launchbox_platform_for_system,
    _load_overmind_config_for_settings,
    _normalize_overmind_link_state,
    _collect_rom_metadata,
    _chunk_rom_metadata_delta,
    _chunk_rom_metadata_inventory,
    _hash_rom_metadata_batches,
    _rom_inventory_fingerprint,
    _empty_rom_metadata_cache,
    _cached_rom_fingerprint_exists,
    _poll_rom_metadata_cache,
    _poll_rom_metadata_once,
    _load_rom_metadata_cache,
    _mark_rom_metadata_upload_clean,
    ROM_INVENTORY_FINGERPRINT_ALGORITHM,
    _persist_rom_metadata_cache,
    _rom_metadata_cache_status,
    _rom_metadata_cache_path,
    _read_pending_rom_metadata_changes,
    _sync_rom_metadata_to_overmind,
    _sample_speed,
    _real_data_roots,
    _peer_ssl_diagnostic,
    _peer_trust_cafile,
    _download_rom_from_peer,
    _download_rom_folder_from_peer,
    _collision_safe_target,
    _rom_fingerprint_exists,
    _best_peer_for_rom,
    _execute_overmind_action,
    _register_or_claim_overmind_token,
    _reclaim_overmind_token_after_unauthorized,
    _collect_emulator_configs,
    _commit_emulator_config_fingerprints,
    _collect_log_sources,
    _commit_log_cursors,
    _collect_game_logs,
    _collect_system_info_payload,
    _is_external_client_ip,
    _unauthenticated_request_allowed,
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
            saved = load_payload(
                database_path_for_legacy_file(Path(tmp) / "credentials.json"),
                "credentials",
                {},
            )
            self.assertIn("password_hash", saved)
            self.assertNotIn("BetterPass123", json.dumps(saved))
            self.assertFalse((Path(tmp) / "credentials.json").exists())
            self.assertFalse(auth.check(f"Basic {default_token}"))
            updated_token = base64.b64encode(b"arcade-admin:BetterPass123").decode("ascii")
            self.assertTrue(auth.check(f"Basic {updated_token}"))

    def test_external_unauthenticated_rate_limit_exempts_private_ips(self) -> None:
        drone_api._UNAUTH_RATE_LIMIT_BUCKETS.clear()
        self.assertFalse(_is_external_client_ip("192.168.1.20"))
        self.assertFalse(_is_external_client_ip("10.0.0.2"))
        self.assertFalse(_is_external_client_ip("127.0.0.1"))
        self.assertTrue(_is_external_client_ip("8.8.8.8"))
        with mock.patch("app.drone_api.DRONE_UNAUTH_RATE_LIMIT_REQUESTS", 2), mock.patch(
            "app.drone_api.DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS", 10
        ):
            self.assertTrue(_unauthenticated_request_allowed("8.8.8.8", now=100.0))
            self.assertTrue(_unauthenticated_request_allowed("8.8.8.8", now=101.0))
            self.assertFalse(_unauthenticated_request_allowed("8.8.8.8", now=102.0))
            self.assertTrue(_unauthenticated_request_allowed("8.8.8.8", now=112.0))
            self.assertTrue(_unauthenticated_request_allowed("192.168.1.20", now=102.0))


class SettingsTests(unittest.TestCase):
    def _write_gamelist(self, system: Path, *roms: str) -> None:
        games = "".join(
            f"<game><path>./{rom}</path><name>{Path(rom).stem}</name></game>"
            for rom in roms
        )
        (system / "gamelist.xml").write_text(f"<gameList>{games}</gameList>\n", encoding="utf-8")

    def test_overmind_error_format_includes_class_when_message_is_blank(self) -> None:
        self.assertEqual(_format_overmind_error(TimeoutError()), "TimeoutError()")
        self.assertIn("URLError reason=", _format_overmind_error(URLError(TimeoutError())))

    def test_hostname_override_builds_reported_drone_url(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"HOSTNAME_OVERRIDE": "bff-drone-a", "HTTPS_PORT": "443"},
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.hostname_override, "bff-drone-a")
        self.assertEqual(_drone_reachable_url(settings, {"ipv4": ["192.168.1.50"]}), "https://bff-drone-a")

    def test_public_swarm_endpoint_can_be_faked_for_local_tests(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "HOSTNAME_OVERRIDE": "bff-drone-b",
                "DRONE_PUBLIC_IP_OVERRIDE": "bff-drone-b",
                "DRONE_ADVERTISED_API_PORT": "8444",
                "DRONE_COMPAT_HTTPS_PORTS": "8444",
                "HTTPS_PORT": "8443",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.https_port, 8443)
        self.assertEqual(settings.advertised_api_port, 8444)
        self.assertEqual(settings.compatibility_https_ports, (8444,))
        self.assertEqual(_drone_reachable_url(settings, {"ipv4": ["172.20.0.10"]}), "https://bff-drone-b:8444")

        with mock.patch("app.drone_api._get_local_ip_addresses", return_value={"ipv4": ["172.20.0.10"], "ipv6": []}):
            network = _drone_network_payload(settings)

        self.assertEqual(network["public_ip"], "bff-drone-b")
        self.assertEqual(network["reachable_url"], "https://bff-drone-b:8444")

    def test_drone_defaults_to_8443_compatibility_listener(self) -> None:
        with mock.patch.dict("os.environ", {"HTTPS_PORT": "443"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.https_port, 443)
        self.assertEqual(settings.compatibility_https_ports, (8443,))

    def test_drone_compatibility_listener_can_be_overridden(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"HTTPS_PORT": "443", "DRONE_COMPAT_HTTPS_PORTS": "8443, 9443, bad, 443"},
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.compatibility_https_ports, (8443, 9443))

    def test_overmind_device_id_persists_after_first_physical_mac_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            device_id_file = root / "system" / "drone-app" / "device-id"
            device_id_file.parent.mkdir(parents=True)
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True), mock.patch(
                "app.drone_api._physical_mac_candidates",
                return_value=["58:47:ca:7e:38:57"],
            ), mock.patch("app.drone_api._runtime_machine_id", return_value="2c:cf:67:97:8c:8f"):
                first = Settings.from_env()

            self.assertEqual(first.overmind_device_id, "58:47:ca:7e:38:57")
            self.assertEqual(device_id_file.read_text(encoding="utf-8").strip(), "58:47:ca:7e:38:57")

            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True), mock.patch(
                "app.drone_api._physical_mac_candidates",
                return_value=["2c:cf:67:97:8c:8f"],
            ), mock.patch("app.drone_api._runtime_machine_id", return_value="aa:bb:cc:dd:ee:ff"):
                restarted = Settings.from_env()

            self.assertEqual(restarted.overmind_device_id, "58:47:ca:7e:38:57")

    def test_configured_overmind_device_id_wins_without_rewriting_persisted_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            device_id_file = root / "system" / "drone-app" / "device-id"
            device_id_file.parent.mkdir(parents=True)
            device_id_file.write_text("58:47:ca:7e:38:57\n", encoding="utf-8")

            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "bff-drone-a"},
                clear=True,
            ):
                settings = Settings.from_env()

            self.assertEqual(settings.overmind_device_id, "bff-drone-a")
            self.assertEqual(device_id_file.read_text(encoding="utf-8").strip(), "58:47:ca:7e:38:57")

    def test_host_preference_order_is_override_ipv4_ipv6(self) -> None:
        with mock.patch.dict("os.environ", {"HOSTNAME_OVERRIDE": "bff-drone-a", "HTTPS_PORT": "443"}, clear=True):
            settings = Settings.from_env()
        network = {"ipv4": ["192.168.1.50"], "ipv6": ["fd00::50"]}
        self.assertEqual(_drone_report_host(settings, network), "bff-drone-a")

        with mock.patch.dict("os.environ", {"HTTPS_PORT": "443"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(_drone_report_host(settings, network), "192.168.1.50")
        self.assertEqual(_drone_report_host(settings, {"ipv6": ["fd00::50"]}), "fd00::50")
        self.assertEqual(_drone_reachable_url(settings, {"ipv6": ["fd00::50"]}), "https://[fd00::50]")

    def test_reachable_url_ignores_loopback_hostname_alias(self) -> None:
        with mock.patch.dict("os.environ", {"HTTPS_PORT": "443"}, clear=True):
            settings = Settings.from_env()
        network = {"ipv4": ["127.0.1.1", "192.168.0.206", "127.0.0.1"], "ipv6": []}

        self.assertEqual(_drone_report_host(settings, network), "192.168.0.206")
        self.assertEqual(_drone_reachable_url(settings, network), "https://192.168.0.206")

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
            "reachable_url": "https://bff-drone-b:443",
            "resolved_network": {"ipv4": ["172.20.0.4"], "ipv6": ["fd00::4"]},
            "api_port": 443,
        }
        self.assertEqual(_peer_address(peer), "https://bff-drone-b:443")

    def test_peer_address_prefers_public_endpoint_for_remote_swarm_transfers(self) -> None:
        peer = {
            "public_reachable_url": "https://198.51.100.20:443",
            "public_ip": "198.51.100.20",
            "reachable_url": "https://192.168.1.20:443",
            "resolved_network": {"ipv4": ["192.168.1.20"]},
            "api_port": 443,
        }
        self.assertEqual(_peer_address(peer), "https://198.51.100.20:443")

    def test_peer_address_builds_public_endpoint_from_public_ip(self) -> None:
        peer = {
            "public_ip": "198.51.100.21",
            "public_resolvable": True,
            "scheme": "https",
            "api_port": 443,
        }
        self.assertEqual(_peer_address(peer), "https://198.51.100.21")

    def test_peer_address_builds_public_endpoint_with_advertised_port(self) -> None:
        peer = {
            "public_ip": "bff-drone-b",
            "public_resolvable": True,
            "scheme": "https",
            "api_port": 8444,
        }
        self.assertEqual(_peer_api_port(peer), 8444)
        self.assertEqual(_peer_address(peer), "https://bff-drone-b:8444")

    def test_peer_address_ignores_unverified_public_ip_when_reachable_url_exists(self) -> None:
        peer = {
            "public_ip": "198.51.100.21",
            "public_resolvable": False,
            "reachable_url": "https://bff-drone-b:443",
            "scheme": "https",
            "api_port": 443,
        }
        self.assertEqual(_peer_address(peer), "https://bff-drone-b:443")

    def test_peer_health_url_uses_public_health_endpoint(self) -> None:
        self.assertEqual(_peer_health_url("https://198.51.100.21"), "https://198.51.100.21/health")
        self.assertEqual(_peer_health_url("https://bff-drone-b:443/"), "https://bff-drone-b:443/health")

    def test_public_ip_peer_probe_checks_health_endpoint_through_peer_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(Path(tmp) / "userdata")}, clear=True):
                settings = Settings.from_env()

            calls = []

            def fake_peer_get_json(url, settings_arg, peer_id=None, config=None, refresh_cert=False):
                calls.append((url, settings_arg, peer_id, config, refresh_cert))
                return {"status": "ok"}

            peer = {"drone_id": "bff-drone-b", "public_ip": "bff-drone-b", "api_port": 8444}
            config = {"overmind_url": "https://overmind.example", "overmind_token": "token"}
            with mock.patch("app.drone_api._peer_get_json", side_effect=fake_peer_get_json):
                result = _probe_peer_public_ip(settings, peer, config=config)

            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["target_address"], "https://bff-drone-b:8444")
            self.assertEqual(result["public_ip"], "bff-drone-b")
            self.assertEqual(result["api_port"], 8444)
            self.assertEqual(calls[0][0], "https://bff-drone-b:8444/health")
            self.assertEqual(calls[0][2], "bff-drone-b")
            self.assertIs(calls[0][3], config)
            self.assertFalse(calls[0][4])

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

    def test_log_source_collection_can_filter_persistent_overmind_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir(parents=True)
            (log_dir / "stderr.log").write_text("drone error\n", encoding="utf-8")
            es_logs = root / "system" / "logs"
            es_logs.mkdir(parents=True)
            (es_logs / "es_launch_stdout.log").write_text("es stdout\n", encoding="utf-8")
            (es_logs / "es_launch_stderr.log").write_text("es stderr\n", encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "LOG_DIR": str(log_dir)},
                clear=True,
            ):
                settings = Settings.from_env()

            payload = _collect_log_sources(
                settings,
                sources=("drone_stderr", "es_launch_stdout", "es_launch_stderr"),
            )
            self.assertEqual(
                {row["source"] for row in payload["logs"]},
                {"drone_stderr", "es_launch_stdout", "es_launch_stderr"},
            )

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

    def test_game_log_collection_detects_launch_with_fingerprint(self) -> None:
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
            self.assertEqual(session["rom_fingerprint"], RomRepository.build_fingerprint(rom))
            self.assertEqual(session["played_at"], "2026-05-26T10:15:00+00:00")

    def test_game_log_collection_detects_batocera_v43_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            roms_root = root / "roms"
            rom = roms_root / "steam" / "243780_PixelJunk_Monsters_Ultimate.sh"
            rom.parent.mkdir(parents=True)
            rom.write_text("#!/bin/sh\n", encoding="utf-8")
            launch_log = root / "system" / "logs" / "es_launch_stdout.log"
            launch_log.parent.mkdir(parents=True)
            launch_log.write_text(
                "\n".join([
                    "2026-06-08 23:25:36,163 DEBUG (emulatorlauncher.py:83):start_rom Running system: steam",
                    "2026-06-08 23:25:36,169 INFO (Emulator.py:128):__post_init__ game settings name: 243780_PixelJunk_Monsters_Ultimate.sh",
                    "2026-06-08 23:25:36,747 DEBUG (emulatorlauncher.py:408):callExternalScripts calling external script: [PosixPath('/usr/share/batocera/configgen/scripts/nvidia-workaround.sh'), 'gameStart', 'steam', 'sh', 'sh', PosixPath('/userdata/roms/steam/243780_PixelJunk_Monsters_Ultimate.sh')]",
                    "2026-06-08 23:25:36,769 DEBUG (emulatorlauncher.py:408):callExternalScripts calling external script: [PosixPath('/usr/share/batocera/configgen/scripts/powermode_launch_hooks.sh'), 'gameStart', 'steam', 'sh', 'sh', PosixPath('/userdata/roms/steam/243780_PixelJunk_Monsters_Ultimate.sh')]",
                ]),
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
            self.assertEqual(session["system_name"], "steam")
            self.assertEqual(session["game_name"], "243780_PixelJunk_Monsters_Ultimate.sh")
            self.assertEqual(session["rom_path"], rom.resolve().as_posix())
            self.assertEqual(session["played_at"], "2026-06-08T23:25:36+00:00")

    def test_collect_emulator_configs_includes_batocera_conf(self) -> None:
        from app.overmind_reporting import collect_emulator_configs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            (root / "system").mkdir(parents=True)
            (root / "system" / "batocera.conf").write_text("system.power.switch=PIN\n", encoding="utf-8")
            retroarch = root / "system" / "configs" / "retroarch"
            retroarch.mkdir(parents=True)
            (retroarch / "retroarchcustom.cfg").write_text("video_driver = vulkan\n", encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()

            payload = collect_emulator_configs(settings, include_unchanged=True)
            rel_paths = {config["relative_path"] for config in payload["configs"]}
            self.assertIn("batocera.conf", rel_paths)
            self.assertIn("retroarch/retroarchcustom.cfg", rel_paths)

    def test_game_event_spool_produces_session_with_duration(self) -> None:
        from app.overmind_game_logs import collect_game_event_sessions, delete_game_event_spool

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            roms_root = root / "roms"
            rom = roms_root / "snes" / "Game.sfc"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"rom-data")
            spool = root / "system" / "drone-app" / "game-events"
            spool.mkdir(parents=True)
            (spool / "1-1-start.json").write_text(
                json.dumps({"event": "start", "played_at": "2026-06-08T10:00:00+00:00", "rom_path": str(rom)}),
                encoding="utf-8",
            )
            (spool / "2-1-end.json").write_text(
                json.dumps({"event": "end", "played_at": "2026-06-08T10:05:00+00:00", "rom_path": str(rom)}),
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(roms_root)},
                clear=True,
            ):
                settings = Settings.from_env()

            sessions, processed = collect_game_event_sessions(settings, RomRepository(roms_root, root / "bios"))
            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session["system_name"], "snes")
            self.assertEqual(session["game_name"], "Game.sfc")
            self.assertEqual(session["rom_path"], rom.resolve().as_posix())
            self.assertEqual(session["rom_fingerprint"], RomRepository.build_fingerprint(rom))
            self.assertEqual(session["played_at"], "2026-06-08T10:00:00+00:00")
            self.assertEqual(session["duration_seconds"], 300)
            self.assertEqual(len(processed), 2)

            # Processed files are removed so they are never re-sent.
            delete_game_event_spool(processed)
            self.assertEqual(list(spool.iterdir()), [])
            self.assertEqual(collect_game_event_sessions(settings, None), ([], []))

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
            requested_snapshot = _collect_emulator_configs(settings, include_unchanged=True)
            self.assertEqual([row["relative_path"] for row in requested_snapshot["configs"]], ["retroarch/retroarch.cfg"])
            self.assertFalse(requested_snapshot["incremental"])
            self.assertEqual(requested_snapshot["configs"][0]["md5"], hashlib.md5(b"video_driver = gl").hexdigest())

            legacy_state = root / "system" / "drone-app" / "overmind_config_fingerprints.json"
            legacy_state.write_text(
                json.dumps({"schema_version": 2, "fingerprints": first["_fingerprints"]}),
                encoding="utf-8",
            )
            with open_database(database_path(root)) as connection:
                connection.execute(
                    "DELETE FROM app_state WHERE namespace = ?",
                    ("overmind_config_fingerprints.json",),
                )
            legacy_retry = _collect_emulator_configs(settings)
            self.assertEqual(legacy_retry["configs"], [])
            self.assertFalse(legacy_state.exists())
            _commit_emulator_config_fingerprints(settings, legacy_retry["_fingerprints"])

            config.write_text("video_driver = vulkan", encoding="utf-8")
            third = _collect_emulator_configs(settings)
            self.assertEqual([row["relative_path"] for row in third["configs"]], ["retroarch/retroarch.cfg"])
            self.assertIn("vulkan", third["configs"][0]["content"])

    def test_emulator_config_collection_uses_allowed_batocera_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            configs = root / "system" / "configs"
            desktop = root / "system" / ".config"
            paths = {
                configs / "dolphin-emu" / "Dolphin.ini": "dolphin",
                configs / "emulationstation" / "es_settings.cfg": "es",
                configs / "rpcs3" / "patches" / "patch.yml": "patch",
                configs / "shadps4" / "user" / "patches" / "enabled.yml": "patch",
                desktop / "pcmanfm" / "default" / "pcmanfm.conf": "desktop",
            }
            excluded = [
                configs / "dolphin-emu" / "TimePlayed.ini",
                configs / "dolphin-emu" / "Logger.ini",
                configs / "rpcs3" / "players_history.yml",
                configs / "rpcs3" / "dev_flash" / "sys.yml",
                configs / "emulationstation" / "scrapers" / "credentials.cfg",
                configs / "shadps4" / "user" / "game_data" / "game.toml",
                configs / "retroarch" / "log" / "runtime.cfg",
                configs / "retroarch" / "logs" / "trace.cfg",
                desktop / "unrelated" / "secret.ini",
            ]
            for path, content in paths.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            for path in excluded:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("do not report", encoding="utf-8")
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()

            result = _collect_emulator_configs(settings)
            rows = {row["relative_path"] for row in result["configs"]}

            self.assertEqual(rows, {
                "dolphin-emu/Dolphin.ini",
                "emulationstation/es_settings.cfg",
                "rpcs3/patches/patch.yml",
                "shadps4/user/patches/enabled.yml",
                "pcmanfm/default/pcmanfm.conf",
            })
            self.assertFalse(any(path.name in str(rows) for path in excluded))

    def test_emulator_config_collection_retries_changed_rows_after_batch_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            configs = root / "system" / "configs" / "retroarch"
            configs.mkdir(parents=True)
            for index in range(251):
                (configs / f"{index:03}.cfg").write_text(str(index), encoding="utf-8")
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()

            first = _collect_emulator_configs(settings)
            self.assertEqual(len(first["configs"]), 250)
            _commit_emulator_config_fingerprints(settings, first["_fingerprints"])
            second = _collect_emulator_configs(settings)

            self.assertEqual([row["relative_path"] for row in second["configs"]], ["retroarch/250.cfg"])

    def test_emulator_config_collection_can_return_full_snapshot_for_local_ui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            configs = root / "system" / "configs" / "retroarch"
            configs.mkdir(parents=True)
            for index in range(251):
                (configs / f"{index:03}.cfg").write_text(str(index), encoding="utf-8")
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()

            snapshot = _collect_emulator_configs(settings, include_unchanged=True, max_configs=0)

            self.assertEqual(len(snapshot["configs"]), 251)
            self.assertFalse(snapshot["incremental"])

    def test_emulator_config_list_and_detail_use_same_selected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            config = root / "system" / "configs" / "retroarch" / "retroarch.cfg"
            config.parent.mkdir(parents=True)
            config.write_text("Renderer = Vulkan", encoding="utf-8")
            backup = root / "system" / "configs" / "retroarch" / "retroarch.cfg.bak"
            backup.write_text("old", encoding="utf-8")
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()

            listing = list_emulator_config_files(settings)
            rows = {row["relative_path"]: row for row in listing["configs"]}

            self.assertEqual(list(rows), ["retroarch/retroarch.cfg"])
            self.assertEqual(rows["retroarch/retroarch.cfg"]["root_name"], "configs")
            self.assertNotIn("content", rows["retroarch/retroarch.cfg"])

            detail = read_emulator_config_file(settings, "configs", "retroarch/retroarch.cfg")
            self.assertEqual(detail["relative_path"], "retroarch/retroarch.cfg")
            self.assertEqual(detail["content"], "Renderer = Vulkan")
            self.assertEqual(detail["md5"], hashlib.md5(b"Renderer = Vulkan").hexdigest())

    def test_emulator_config_list_matches_overmind_default_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            configs = root / "system" / "configs" / "retroarch"
            configs.mkdir(parents=True)
            for index in range(251):
                (configs / f"{index:03}.cfg").write_text(str(index), encoding="utf-8")
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()

            listing = list_emulator_config_files(settings)

            self.assertEqual(listing["count"], 250)
            self.assertEqual(listing["max_configs"], 250)

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

    def test_drone_client_ssl_context_loads_client_cert_for_mtls_peer_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            cert_file = Path(tmp) / "drone.crt"
            key_file = Path(tmp) / "drone.key"
            ca_file = Path(tmp) / "peer.crt"
            cert_file.write_text("client-cert", encoding="utf-8")
            key_file.write_text("client-key", encoding="utf-8")
            ca_file.write_text("peer-cert", encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "DRONE_MTLS_ENABLED": "true",
                    "DRONE_CERT_FILE": str(cert_file),
                    "DRONE_KEY_FILE": str(key_file),
                },
                clear=True,
            ):
                settings = Settings.from_env()

            class FakeContext:
                def __init__(self):
                    self.loaded_chain = None
                    self.check_hostname = True

                def load_cert_chain(self, certfile=None, keyfile=None):
                    self.loaded_chain = (certfile, keyfile)

            fake_context = FakeContext()
            with mock.patch("app.drone_api.ssl.create_default_context", return_value=fake_context):
                context = drone_api._drone_client_ssl_context(settings, "https://198.51.100.21/health", verify=True, cafile=ca_file)

            self.assertIs(context, fake_context)
            self.assertEqual(fake_context.loaded_chain, (str(cert_file), str(key_file)))
            self.assertFalse(fake_context.check_hostname)

    def test_peer_ssl_diagnostic_identifies_hostname_mismatch(self) -> None:
        diagnostic = _peer_ssl_diagnostic(
            "https://bff-drone-b:443/health",
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
                "reachable_url": "https://bff-drone-b:443",
                "resolved_network": {"ipv4": ["172.20.0.4"]},
            }
            with mock.patch("app.drone_api._drone_client_ssl_context", side_effect=fake_context), mock.patch(
                "app.drone_api.urlopen", side_effect=fake_urlopen
            ), mock.patch("app.drone_api._fetch_peer_certificate") as fetch:
                result = _download_rom_from_peer(settings, {}, peer, "atari7800", "Asteroids (USA).zip", expected_size=7)

            self.assertEqual(result["source_drone_id"], "bff-drone-b")
            self.assertEqual(requests[0][0], "https://bff-drone-b:443/v1/api/peer/roms/atari7800/Asteroids%20%28USA%29.zip")
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

    def test_download_folder_rom_from_peer_recreates_tree_without_fingerprint(self) -> None:
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
            ), mock.patch.object(RomRepository, "build_fingerprint", side_effect=AssertionError("folder sync should not hash")):
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

    def test_download_manager_retries_failed_or_cancelled_job(self) -> None:
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

            queued = manager.enqueue_rom({"overmind_url": "https://overmind.local"}, {"drone_id": "source-a"}, "fbneo", "1943.zip", expected_size=123)
            manager.cancel(queued["job_id"])
            retry = manager.retry(queued["job_id"])

            self.assertEqual(retry["status"], "queued")
            self.assertNotEqual(retry["job"]["job_id"], queued["job_id"])
            self.assertEqual(retry["job"]["file_path"], "1943.zip")
            self.assertEqual(retry["job"]["system"], "fbneo")
            self.assertEqual(retry["job"]["retried_from_job_id"], queued["job_id"])
            snapshot = manager.snapshot()
            self.assertEqual(len(snapshot["queued"]), 1)
            self.assertEqual(snapshot["queued"][0]["job_id"], retry["job"]["job_id"])

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
            queued = manager.enqueue_rom(config, {"drone_id": "source-a"}, "snes", "Game.zip", expected_size=8, expected_fingerprint="abc")
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
                "rom_fingerprint": "abc",
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
            self.assertEqual(pushed["rom_fingerprint"], "abc")

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
                            "public_reachable_url": "https://198.51.100.19:443",
                            "rom_systems": ["snes"],
                            "last_speed_sample": {"upload_mbps": 500},
                        },
                        {
                            "device_id": "source-with-rom",
                            "online": True,
                            "public_resolvable": True,
                            "public_reachable_url": "https://198.51.100.20:443",
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

    def test_sync_system_posts_failed_activity_with_payload_sync_id(self) -> None:
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
            action = {
                "id": "action-1",
                "action": "sync_system",
                "payload": {
                    "system_name": "fbneo",
                    "roms": [{
                        "sync_id": "sync-row-1",
                        "system_name": "fbneo",
                        "file_path": "1943.zip",
                        "devices": [{"device_id": "missing-source"}],
                    }],
                },
            }
            config = {"overmind_url": "https://overmind.local", "overmind_token": "drone-token"}

            with mock.patch("app.drone_api._get_download_manager") as manager, mock.patch(
                "app.drone_api._best_peer_for_rom", return_value=None
            ), mock.patch("app.drone_api._post_rom_sync_activity") as post_activity:
                manager.return_value = object()
                status, message, result = _execute_overmind_action(settings, repo, action, config, "https://overmind.local", "drone-token")

            self.assertEqual(status, "failed")
            self.assertIn("ROM sync failed", message)
            pushed = post_activity.call_args.args[2]
            self.assertEqual(pushed["sync_id"], "sync-row-1")
            self.assertEqual(pushed["status"], "failed")
            self.assertEqual(result["activity"][0]["sync_id"], "sync-row-1")

    def test_cached_rom_fingerprint_exists_uses_metadata_cache_without_scanning_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                },
                clear=True,
            ):
                settings = Settings.from_env()
            cache = _empty_rom_metadata_cache()
            cache["entries"] = {
                "fbneo:game.zip": {
                    "system": "fbneo",
                    "file_path": "game.zip",
                    "rom_fingerprint": "abc123",
                }
            }
            _persist_rom_metadata_cache(settings, cache, rom_updates=cache["entries"], queue_changes=False)

            self.assertTrue(_cached_rom_fingerprint_exists(settings, "ABC123"))
            self.assertFalse(_cached_rom_fingerprint_exists(settings, "missing"))

    def test_rom_inventory_fingerprint_is_stable_for_equivalent_rom_sets(self) -> None:
        left = _rom_inventory_fingerprint([
            {"system": "SNES", "file_path": "./A\\Game.zip", "rom_fingerprint": "ABC", "file_size": 12},
            {"system_name": "nes", "rom_path": "B.zip", "fingerprint": "def", "file_size": 4},
        ])
        right = _rom_inventory_fingerprint([
            {"system": "nes", "file_path": "b.zip", "rom_fingerprint": "DEF", "file_size": 4, "modified_time": 999},
            {"system": "snes", "relative_path": "a/game.zip", "fingerprint": "abc", "file_size": 12},
        ])

        self.assertEqual(left, right)

    def test_rom_metadata_inventory_payloads_include_final_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
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
            snapshot = {
                "type": "asset_metadata",
                "collected_at": "2026-06-04T12:00:00+00:00",
                "systems": [{"name": "snes", "rom_count": 2}],
                "roms": [
                    {"system": "snes", "file_path": "A.zip", "rom_fingerprint": "aaa", "file_size": 1},
                    {"system": "snes", "file_path": "B.zip", "rom_fingerprint": "bbb", "file_size": 2},
                ],
            }
            expected = _rom_inventory_fingerprint(snapshot["roms"])

            payloads = _chunk_rom_metadata_inventory(settings, snapshot, chunk_size=1, replace_all=True)

            self.assertEqual(len(payloads), 2)
            self.assertFalse(payloads[0]["inventory_complete"])
            self.assertTrue(payloads[-1]["inventory_complete"])
            self.assertEqual(payloads[-1]["rom_inventory_fingerprint"], expected)
            self.assertEqual(payloads[-1]["rom_inventory_fingerprint_algorithm"], ROM_INVENTORY_FINGERPRINT_ALGORITHM)

    def test_rom_metadata_delta_payloads_include_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
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
            snapshot = {
                "type": "asset_metadata",
                "collected_at": "2026-06-04T12:00:00+00:00",
                "systems": [{"name": "snes", "rom_count": 1}],
                "roms": [{"system": "snes", "file_path": "A.zip", "rom_fingerprint": "aaa", "file_size": 1}],
            }
            changes = {"roms": snapshot["roms"], "deleted": {"roms": []}}

            payloads = _chunk_rom_metadata_delta(settings, snapshot, changes, chunk_size=10)

            self.assertEqual(len(payloads), 1)
            self.assertTrue(payloads[0]["inventory_complete"])
            self.assertEqual(payloads[0]["rom_inventory_fingerprint"], _rom_inventory_fingerprint(snapshot["roms"]))

    def test_mark_rom_metadata_upload_clean_persists_fingerprint_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                },
                clear=True,
            ):
                settings = Settings.from_env()

            _mark_rom_metadata_upload_clean(settings, "abc123")
            cache, _ = _load_rom_metadata_cache(settings)

            self.assertEqual(cache["rom_inventory_fingerprint"], "abc123")
            self.assertEqual(cache["rom_inventory_fingerprint_algorithm"], ROM_INVENTORY_FINGERPRINT_ALGORITHM)

    def test_disk_rom_without_gamelist_is_listed_with_fingerprint(self) -> None:
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
            self.assertEqual(roms[0]["fingerprint"], RomRepository.build_fingerprint(rom))

    def test_gamelist_rom_metadata_does_not_duplicate_disk_rows_when_gamelist_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Game.zip").write_bytes(b"rom")
            (system / "gamelist.xml").write_text(
                "<gameList><game><path>./Game.zip</path><name>Gamelist Game</name></game></gameList>\n",
                encoding="utf-8",
            )
            repo = RomRepository(root / "roms", root / "bios")

            _, roms = repo.list_gamelist_rom_metadata("snes")

            self.assertEqual(len(roms), 1)
            self.assertEqual(roms[0]["file_path"], "Game.zip")
            self.assertTrue(roms[0]["has_gamelist_entry"])
            self.assertEqual(roms[0]["metadata_source"], "gamelist.xml")

    def test_gamelist_rom_metadata_includes_disk_rows_missing_from_stale_gamelist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            system = root / "roms" / "fbneo"
            system.mkdir(parents=True)
            (system / "Known.zip").write_bytes(b"rom")
            (system / "1943.zip").write_bytes(b"new-rom")
            (system / "gamelist.xml").write_text(
                "<gameList><game><path>./Known.zip</path><name>Known Game</name></game></gameList>\n",
                encoding="utf-8",
            )
            repo = RomRepository(root / "roms", root / "bios")

            _, roms = repo.list_gamelist_rom_metadata("fbneo")

            by_path = {row["file_path"]: row for row in roms}
            self.assertEqual(set(by_path), {"Known.zip", "1943.zip"})
            self.assertTrue(by_path["Known.zip"]["has_gamelist_entry"])
            self.assertFalse(by_path["1943.zip"]["has_gamelist_entry"])
            self.assertEqual(by_path["1943.zip"]["metadata_source"], "filesystem")

    def test_gamelist_rom_metadata_uses_disk_rows_when_gamelist_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rom = root / "roms" / "fbneo" / "1942.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"rom")
            repo = RomRepository(root / "roms", root / "bios")

            _, roms = repo.list_gamelist_rom_metadata("fbneo")

            self.assertEqual(len(roms), 1)
            self.assertEqual(roms[0]["file_path"], "1942.zip")
            self.assertFalse(roms[0]["has_gamelist_entry"])
            self.assertEqual(roms[0]["metadata_source"], "filesystem")

    def test_rom_list_can_skip_fingerprint_for_fast_ui_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rom = root / "roms" / "snes" / "Loose Game (USA).zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"loose-rom")
            repo = RomRepository(root / "roms", root / "bios")

            with mock.patch.object(RomRepository, "build_fingerprint", side_effect=AssertionError("should not hash")):
                _, roms = repo.list_assets("snes", "roms", include_fingerprint=False)

            self.assertEqual(len(roms), 1)
            self.assertNotIn("fingerprint", roms[0])
            self.assertNotIn("rom_fingerprint", roms[0])
            self.assertEqual(roms[0]["rom_path"], "Loose Game (USA).zip")

    def test_ps3_folder_rom_is_listed_without_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / "roms" / "ps3" / "Demon Souls.ps3"
            (game / "PS3_GAME" / "USRDIR").mkdir(parents=True)
            (game / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").write_bytes(b"boot")
            (game / "PS3_DISC.SFB").write_bytes(b"disc")
            repo = RomRepository(root / "roms", root / "bios")

            with mock.patch.object(RomRepository, "build_fingerprint", side_effect=AssertionError("folder ROM should not hash")):
                _, roms = repo.list_assets("ps3", "roms")

            self.assertEqual(len(roms), 1)
            self.assertEqual(roms[0]["entry_type"], "folder")
            self.assertFalse(roms[0]["is_downloadable"])
            self.assertEqual(roms[0]["file_path"], "Demon Souls.ps3")
            self.assertEqual(roms[0]["file_size"], 8)
            self.assertNotIn("fingerprint", roms[0])
            self.assertNotIn("rom_fingerprint", roms[0])

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

    def test_fingerprint_identity_and_collision_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            existing = system / "Asteroids (USA).zip"
            existing.write_bytes(b"one")
            (system / "Renamed Asteroids.zip").write_bytes(b"one")
            repo = RomRepository(root / "roms", root / "bios")

            self.assertTrue(_rom_fingerprint_exists(repo, RomRepository.build_fingerprint(existing)))
            self.assertEqual(_collision_safe_target(system, "Asteroids (USA).zip").name, "Asteroids (USA) (2).zip")

    def test_build_fingerprint_is_sampled_deterministic_and_size_sensitive(self) -> None:
        import app.drone_api as da
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a.bin"
            b = root / "b.bin"
            # Identical content -> identical fingerprint, and it is stable across calls.
            a.write_bytes(b"identical-content")
            b.write_bytes(b"identical-content")
            self.assertEqual(RomRepository.build_fingerprint(a), RomRepository.build_fingerprint(b))
            self.assertEqual(RomRepository.build_fingerprint(a), RomRepository.build_fingerprint(a))
            # A small content change flips the fingerprint (small files are hashed whole).
            b.write_bytes(b"identical-contenX")
            self.assertNotEqual(RomRepository.build_fingerprint(a), RomRepository.build_fingerprint(b))

            # Large files: only the sample windows are read, but size is folded in so two
            # files of different size never collide even if their samples coincide.
            sample = da.FINGERPRINT_SAMPLE_BYTES
            big = root / "big.bin"
            bigger = root / "bigger.bin"
            body = b"\x00" * (sample * 4)
            big.write_bytes(body)
            bigger.write_bytes(body + b"\x00")  # same head/mid/tail samples, different size
            self.assertNotEqual(RomRepository.build_fingerprint(big), RomRepository.build_fingerprint(bigger))

            # A change confined to the unsampled middle of a large file is intentionally
            # NOT detected (documents the sampled-hash trade-off).
            mid_changed = root / "mid.bin"
            altered = bytearray(body)
            altered[sample] = 0xFF  # well outside head/mid/tail windows of a 4*sample file
            mid_changed.write_bytes(bytes(altered))
            # (size identical, samples identical) -> same fingerprint by design.
            self.assertEqual(RomRepository.build_fingerprint(big), RomRepository.build_fingerprint(mid_changed))

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
            batocera_conf = root / "system" / "batocera.conf"
            control_dir = root / "system" / "drone-app" / "control"
            batocera_conf.parent.mkdir(parents=True)
            batocera_conf.write_text("global.retroachievements=1\nkiosk.enabled=0\n", encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "BATOCERA_CONF_FILE": str(batocera_conf),
                    "DRONE_SERVICE_CONTROL_DIR": str(control_dir),
                },
                clear=True,
            ):
                settings = Settings.from_env()
                repo = RomRepository(root / "roms", root / "bios")

                with mock.patch("app.drone_api.subprocess.Popen") as popen:
                    enabled_status, enabled_message, enabled_result = _execute_overmind_action(
                        settings, repo, {"action": "enable_kiosk"}
                    )
                    self.assertIn("kiosk.enabled=1", batocera_conf.read_text(encoding="utf-8"))
                    disabled_status, disabled_message, disabled_result = _execute_overmind_action(
                        settings, repo, {"action": "disable_kiosk"}
                    )

            self.assertEqual(enabled_status, "completed")
            self.assertIn("Kiosk mode enabled", enabled_message)
            self.assertTrue(enabled_result["enabled"])
            self.assertEqual(disabled_status, "completed")
            self.assertIn("Kiosk mode disabled", disabled_message)
            self.assertFalse(disabled_result["enabled"])
            self.assertIn("global.retroachievements=1", batocera_conf.read_text(encoding="utf-8"))
            self.assertIn("kiosk.enabled=0", batocera_conf.read_text(encoding="utf-8"))
            self.assertTrue((control_dir / "restart-emulationstation.request").exists())
            popen.assert_not_called()

    def test_refresh_emulator_list_restarts_emulationstation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control_dir = root / "system" / "drone-app" / "control"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "DRONE_SERVICE_CONTROL_DIR": str(control_dir)},
                clear=True,
            ):
                settings = Settings.from_env()
                repo = RomRepository(root / "roms", root / "bios")
                with mock.patch("app.drone_api.subprocess.Popen") as popen:
                    status, message, result = _execute_overmind_action(settings, repo, {"action": "refresh_emulator_list"})

            self.assertEqual(status, "completed")
            self.assertIn("Emulator list refresh", message)
            self.assertEqual(result["type"], "emulator_list_refresh")
            self.assertTrue(result["emulationstation_restarted"])
            self.assertTrue((control_dir / "restart-emulationstation.request").exists())
            popen.assert_not_called()

    def test_remote_restart_action_is_deferred_to_root_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()
            repo = RomRepository(root / "roms", root / "bios")
            with mock.patch("app.drone_api.subprocess.Popen") as popen:
                status, message, result = _execute_overmind_action(settings, repo, {"action": "restart"})

            self.assertEqual(status, "completed")
            self.assertIn("service supervisor", message)
            self.assertEqual(result["type"], "system_restart")
            self.assertTrue(result["reboot_requested"])
            self.assertEqual(result["exit_code"], 76)
            popen.assert_not_called()

    def test_rebuild_asset_metadata_action_queues_without_blocking_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)
            _persist_rom_metadata_cache(
                settings,
                {
                    **_empty_rom_metadata_cache(),
                    "entries": {
                        "snes/chrono.zip": {
                            "system": "snes",
                            "file_path": "chrono.zip",
                            "rom_name": "Chrono",
                            "file_size": 12,
                        }
                    },
                    "systems": [{"name": "snes", "rom_count": 1}],
                    "dirty": False,
                    "full_refresh_pending": False,
                },
                rom_updates={
                    "snes/chrono.zip": {
                        "system": "snes",
                        "file_path": "chrono.zip",
                        "rom_name": "Chrono",
                        "file_size": 12,
                    }
                },
            )
            state_db = database_path(settings.userdata_root)
            save_payload(state_db, "overmind", {"overmind_token": "keep-me"})
            drone_api._ROM_METADATA_WAKE.clear()

            with mock.patch("app.drone_api._sync_rom_metadata_to_overmind", side_effect=AssertionError("heartbeat thread must not rebuild inline")):
                status, message, result = _execute_overmind_action(
                    settings,
                    repo,
                    {"action": "rebuild_asset_metadata", "id": "rebuild-1"},
                    {},
                    "https://overmind.local",
                    "drone-token",
                )

            cache, _ = _load_rom_metadata_cache(settings)
            self.assertEqual(load_payload(state_db, "overmind", {}), {"overmind_token": "keep-me"})
            self.assertEqual(status, "completed")
            self.assertIn("local asset cache was cleared", message)
            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["reason"], "local_asset_cache_cleared")
            self.assertTrue(result["poller_wake_requested"])
            self.assertTrue(drone_api._ROM_METADATA_WAKE.is_set())
            self.assertTrue(cache["dirty"])
            self.assertTrue(cache["full_refresh_pending"])
            self.assertEqual(cache["entries"], {})
            self.assertEqual(cache["systems"], [])

    def test_purge_asset_cache_action_clears_entries_but_preserves_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)
            seeded_entry = {
                "system": "snes",
                "file_path": "chrono.zip",
                "rom_name": "Chrono",
                "file_size": 12,
                "fingerprint": "abc123",
                "rom_fingerprint": "abc123",
            }
            _persist_rom_metadata_cache(
                settings,
                {
                    **_empty_rom_metadata_cache(),
                    "entries": {"snes:chrono.zip": seeded_entry},
                    "systems": [{"name": "snes", "rom_count": 1}],
                    "dirty": False,
                    "full_refresh_pending": False,
                },
                rom_updates={"snes:chrono.zip": seeded_entry},
            )
            drone_api._ROM_METADATA_WAKE.clear()

            with mock.patch("app.drone_api._sync_rom_metadata_to_overmind", side_effect=AssertionError("heartbeat thread must not resync inline")):
                status, message, result = _execute_overmind_action(
                    settings,
                    repo,
                    {"action": "purge_asset_cache", "id": "purge-1"},
                    {},
                    "https://overmind.local",
                    "drone-token",
                )

            cache, _ = _load_rom_metadata_cache(settings)
            self.assertEqual(status, "completed")
            self.assertIn("fingerprint values were kept", message)
            self.assertEqual(result["reason"], "full_refresh_kept_fingerprint")
            self.assertTrue(result["poller_wake_requested"])
            self.assertTrue(drone_api._ROM_METADATA_WAKE.is_set())
            # Resync is queued ...
            self.assertTrue(cache["dirty"])
            self.assertTrue(cache["full_refresh_pending"])
            # ... the cached ROM entries are actually cleared (count -> 0) ...
            self.assertEqual(cache["entries"], {})
            # ... but the fingerprint is preserved so the rebuild does not re-fingerprint.
            preserved = drone_api._read_preserved_asset_fingerprint(settings)
            self.assertIn("snes:chrono.zip", preserved["rom"])
            self.assertEqual(preserved["rom"]["snes:chrono.zip"]["fingerprint"], "abc123")

    def test_metadata_upload_snapshot_uses_cached_rows_without_gamelist_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            cache = {
                **_empty_rom_metadata_cache(),
                "last_full_scan_at": "2026-01-01T00:00:00+00:00",
                "entries": {
                    "snes/chrono.zip": {
                        "system": "snes",
                        "file_path": "chrono.zip",
                        "rom_name": "Chrono",
                        "gamelist_path": str(root / "roms" / "snes" / "gamelist.xml"),
                        "gamelist_game_id": "chrono.zip",
                    }
                },
            }

            with mock.patch("app.drone_api._gamelist_metadata_for_reference", side_effect=AssertionError("upload snapshot should not parse gamelists")):
                snapshot = drone_api._build_rom_metadata_snapshot_from_cache(settings, cache)

            self.assertEqual(snapshot["roms"][0]["rom_name"], "Chrono")
            self.assertTrue(snapshot["roms"][0]["has_gamelist_entry"])
            self.assertEqual(snapshot["roms"][0]["metadata_source"], "gamelist.xml")

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

    def test_drone_overmind_client_uses_typed_overmind_endpoints(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath("app/drone_api.py").read_text(encoding="utf-8")
        reporting_source = Path(__file__).resolve().parents[1].joinpath("app/overmind_reporting.py").read_text(encoding="utf-8")
        game_log_source = Path(__file__).resolve().parents[1].joinpath("app/overmind_game_logs.py").read_text(encoding="utf-8")

        for endpoint in [
            "/api/devices/{device_id}/heartbeat",
            "/api/devices/{device_id}/rom-metadata",
            "/api/devices/{device_id}/downloads",
            "/api/devices/{device_id}/speed",
            "/api/devices/{device_id}/events",
            "/api/devices/{device_id}/peer-checks",
            "/api/devices/{device_id}/game-logs",
            "/api/devices/{device_id}/log-sources",
            "/api/devices/{device_id}/emulator-configs",
            "/api/devices/{device_id}/actions/{action_id}/complete",
        ]:
            self.assertIn(endpoint, source)
        self.assertIn('"type": "asset_metadata"', source)
        self.assertIn('"type": "game_logs"', game_log_source)
        self.assertIn('"type": "log_sources"', reporting_source)
        self.assertIn('"type": "emulator_configs"', reporting_source)

    def test_admin_ui_exposes_drone_self_update_action(self) -> None:
        api_routes = Path(__file__).resolve().parents[1].joinpath("app/api_routes.py").read_text(encoding="utf-8")
        js_source = Path(__file__).resolve().parents[1].joinpath("app/static/js/drone.js").read_text(encoding="utf-8")
        drone_source = Path(__file__).resolve().parents[1].joinpath("app/drone_api.py").read_text(encoding="utf-8")

        self.assertIn('parts[1] == "system" and parts[2] == "update-drone"', api_routes)
        self.assertIn("async function updateDroneApp()", js_source)
        self.assertIn('apiPost("/admin/system/update-drone"', js_source)
        self.assertIn("DRONE_LATEST_ARCHIVE_URL", drone_source)
        self.assertIn("os.execv(sys.executable, [sys.executable, *sys.argv])", drone_source)
        self.assertIn("os._exit(DRONE_SELF_UPDATE_EXIT_CODE)", drone_source)

    def test_drone_update_overlays_release_files_without_deleting_app_tree(self) -> None:
        drone_source = Path(__file__).resolve().parents[1].joinpath("app/drone_api.py").read_text(encoding="utf-8")

        self.assertIn("def _overlay_drone_release_tree", drone_source)
        self.assertIn('if "__pycache__" in relative.parts or item.name.endswith(".pyc"):', drone_source)
        self.assertNotIn("shutil.rmtree(target)", drone_source)
        self.assertNotIn("shutil.copy2(item, destination)", drone_source)
        self.assertIn("shutil.copyfile(item, destination)", drone_source)
        self.assertIn("copied_files", drone_source)

    def test_mame_config_source_accepts_batocera_cfg_directory(self) -> None:
        drone_source = Path(__file__).resolve().parents[1].joinpath("app/drone_api.py").read_text(encoding="utf-8")

        self.assertIn('"/userdata/system/configs/mame/default.cfg"', drone_source)
        self.assertIn('"/userdata/system/configs/mame"', drone_source)

    def test_route_mixins_export_for_package_startup(self) -> None:
        api_routes = importlib.import_module("app.api_routes")
        ui_routes = importlib.import_module("app.ui_routes")
        drone_api = importlib.import_module("app.drone_api")

        self.assertTrue(hasattr(api_routes, "ApiRoutesMixin"))
        self.assertTrue(hasattr(ui_routes, "UiRoutesMixin"))
        self.assertIs(drone_api.ApiRoutesMixin, api_routes.ApiRoutesMixin)
        self.assertIs(drone_api.UiRoutesMixin, ui_routes.UiRoutesMixin)

    def test_startup_scripts_validate_local_app_before_launch(self) -> None:
        root = Path(__file__).resolve().parents[1]
        installer = root.joinpath("scripts/batocera_install.sh").read_text(encoding="utf-8")
        uninstaller = root.joinpath("scripts/batocera_uninstall.sh").read_text(encoding="utf-8")
        run_now = root.joinpath("scripts/run_now.sh").read_text(encoding="utf-8")
        drone_source = root.joinpath("app/drone_api.py").read_text(encoding="utf-8")

        self.assertIn("validate_local_app()", installer)
        self.assertIn("missing or empty ${required_file}", installer)
        self.assertIn("Local Drone app import check failed; downloading a fresh app bundle.", installer)
        self.assertIn('"app.ui_routes": "UiRoutesMixin"', installer)
        self.assertIn("DRONE_APP_STAGE_ONLY=1", installer)
        self.assertIn("✓ Updated Drone app bundle", installer)
        self.assertIn("Restarting Drone service with updated app bundle", installer)
        self.assertIn("DRONE_REMOTE_REBOOT_EXIT_CODE", installer)
        self.assertIn("request_host_reboot()", installer)
        self.assertIn("service_control_worker()", installer)
        self.assertIn("/etc/init.d/S31emulationstation restart", installer)
        self.assertIn("DRONE_SERVICE_CONTROL_DIR", drone_source)
        self.assertIn("ensure_dns_fallback()", installer)
        self.assertIn("nameserver 1.1.1.1", installer)
        self.assertIn("/userdata/system/drone-app/rom_metadata_cache.sqlite3*", installer)
        self.assertIn('chown root:"$DRONE_GROUP" /userdata/system/batocera.conf', installer)
        self.assertIn("chmod 664 /userdata/system/batocera.conf", installer)
        self.assertIn("DRONE_REPAIR_ROM_PERMISSIONS:-0", installer)
        self.assertIn("DRONE_UNAUTH_RATE_LIMIT_ENABLED='${DRONE_UNAUTH_RATE_LIMIT_ENABLED:-1}'", installer)
        self.assertIn("DRONE_UNAUTH_RATE_LIMIT_REQUESTS='${DRONE_UNAUTH_RATE_LIMIT_REQUESTS:-60}'", installer)
        self.assertIn("DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS='${DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS:-60}'", installer)
        self.assertIn('DRONE_UNAUTH_RATE_LIMIT_ENABLED="${DRONE_UNAUTH_RATE_LIMIT_ENABLED:-1}"', run_now)
        self.assertIn('DRONE_UNAUTH_RATE_LIMIT_REQUESTS="${DRONE_UNAUTH_RATE_LIMIT_REQUESTS:-60}"', run_now)
        self.assertIn('DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS="${DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS:-60}"', run_now)
        self.assertIn("ROM_METADATA_HASH_ROMS_ENABLED='${ROM_METADATA_HASH_ROMS_ENABLED:-1}'", installer)
        self.assertIn('ROM_METADATA_HASH_ROMS_ENABLED="${ROM_METADATA_HASH_ROMS_ENABLED:-1}"', run_now)
        self.assertIn("ROM_METADATA_UPLOAD_CHUNK_SIZE='${ROM_METADATA_UPLOAD_CHUNK_SIZE:-250}'", installer)
        self.assertIn('ROM_METADATA_UPLOAD_CHUNK_SIZE="${ROM_METADATA_UPLOAD_CHUNK_SIZE:-250}"', run_now)
        self.assertIn("DRONE_LOG_UNAUTHORIZED_REQUESTS='${DRONE_LOG_UNAUTHORIZED_REQUESTS:-0}'", installer)
        self.assertIn('DRONE_LOG_UNAUTHORIZED_REQUESTS="${DRONE_LOG_UNAUTHORIZED_REQUESTS:-0}"', run_now)
        self.assertIn('echo "[drone-service] Downloading and launching Drone app..."\n  wait_for_network', installer)
        self.assertNotIn("ensure_permissions\n    wait_for_network\n\n    supervise_drone", installer)
        self.assertIn("Missing or empty required file", run_now)
        self.assertIn("Downloaded Drone App failed import validation", run_now)
        self.assertIn('"app.api_routes": "ApiRoutesMixin"', run_now)
        self.assertIn("import shutil", run_now)
        self.assertIn("Drone App staged successfully", run_now)
        self.assertEqual(run_now.count("source = archive.extractfile(member)"), 1)
        self.assertIn("/userdata/system/services/DRONE_SERVER", uninstaller)
        self.assertIn("/userdata/system/services/DRONE_APP", uninstaller)
        self.assertIn("/userdata/system/custom.sh", uninstaller)
        self.assertIn("remove_legacy_custom_sh_block()", uninstaller)
        self.assertIn("ROM files, artwork folders, gamelist.xml files", uninstaller)

    def test_home_page_does_not_block_on_speed_test(self) -> None:
        root = Path(__file__).resolve().parents[1]
        js_source = root.joinpath("app/static/js/drone.js").read_text(encoding="utf-8")
        api_routes = root.joinpath("app/api_routes.py").read_text(encoding="utf-8")
        drone_source = root.joinpath("app/drone_api.py").read_text(encoding="utf-8")

        self.assertIn("loadSystemInfoBar();", js_source)
        self.assertNotIn("await loadSystemInfoBar();", js_source)
        self.assertIn("const data = await getSystemsData();", js_source)
        self.assertNotIn("getSystemsData(),\n        refreshRandomThemeLogo(),", js_source)
        self.assertIn('api("/admin/system-info?speed=1")', js_source)
        self.assertIn("include_speed = ", api_routes)
        self.assertIn("def _handle_admin_system_info(self, include_speed: bool = False)", drone_source)
        self.assertIn("_sample_speed() if include_speed else", drone_source)
        self.assertNotIn('id="${prefix}FilterToggle" data-bs-toggle="dropdown"', js_source)
        self.assertIn('event.stopPropagation();', js_source)
        self.assertIn('class="table table-hover align-middle themed-table bios-table"', js_source)
        self.assertIn('const selected = themeFilterInitialized && !(themeFilterSelectedSystems || []).length ? ["__none__"]', js_source)
        self.assertIn('class="system-health-row"', js_source)
        self.assertIn("emulatorConfigSelectionRequestId", js_source)
        self.assertIn("document.activeElement !== versionSelect", js_source)
        self.assertIn('apiPost("/admin/asset-cache/clear-pending"', js_source)
        self.assertIn("What this means:", js_source)

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

    def test_approved_overmind_token_clears_stale_pending_swarm_status(self) -> None:
        config = {
            "overmind_token": "approved-drone-token",
            "integration_enabled": True,
            "integration_state": "pending_approval",
            "swarm_connection_status": "pending approval",
            "notes": "Psionic connection detected. Awaiting Overlord approval.",
        }

        changed = _normalize_overmind_link_state(config)

        self.assertTrue(changed)
        self.assertEqual(config.get("integration_state"), "polling")
        self.assertEqual(config.get("swarm_connection_status"), "connected")
        self.assertEqual(config.get("notes"), "Drone approved by Overmind and polling is active.")

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
            attempt = saved.get("last_onboarding_attempt") or {}
            self.assertEqual(attempt.get("endpoint"), "https://bff-overmind:8000/api/devices/register")
            self.assertEqual(attempt.get("device_id"), "bff-drone-b")
            self.assertTrue(attempt.get("auth_token_present"))
            self.assertEqual(attempt.get("auth_token_fingerprint"), hashlib.sha256(b"shared-token").hexdigest()[:12])
            self.assertTrue(attempt.get("payload_authorization_token_present"))
            self.assertFalse(swarm_path.exists())
            self.assertEqual(load_payload(database_path(root), "overmind_swarm.json", None), [])

    def test_gpu_info_tolerates_unavailable_detection(self) -> None:
        with mock.patch("app.drone_api.subprocess.run", side_effect=FileNotFoundError()):
            info = _collect_gpu_info()
        self.assertIn("vendor", info)
        self.assertIn("pci_devices", info)

    def test_system_info_includes_performance_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            batocera_conf = root / "system" / "batocera.conf"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root), "BATOCERA_CONF_FILE": str(batocera_conf)}, clear=True):
                settings = Settings.from_env()
            settings.batocera_conf_file.parent.mkdir(parents=True, exist_ok=True)
            settings.batocera_conf_file.write_text("kiosk.enabled=1\n", encoding="utf-8")

            info = _collect_system_info_payload(settings)

            self.assertIn("performance", info)
            self.assertIn("cpu", info["performance"])
            self.assertIn("memory", info["performance"])
            self.assertIn("disk", info["performance"])
            self.assertIs(info["kiosk_enabled"], True)

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
            self.assertTrue(result["bios"][0]["bios_md5"])

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

    def test_rom_metadata_cache_reuses_fingerprint_and_detects_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"first")
            self._write_gamelist(rom.parent, "Game.zip")
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
            self.assertNotIn("rom_fingerprint", snapshot["roms"][0])
            first_bios_md5 = snapshot["bios"][0]["bios_md5"]
            with mock.patch("app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", True):
                patches = list(_hash_rom_metadata_batches(settings, repo, batch_size=1))
            self.assertEqual(len(patches), 1)
            first_fingerprint = patches[0]["roms"][0]["rom_fingerprint"]
            cache_data, _ = _load_rom_metadata_cache(settings)
            cache_data["dirty"] = False
            _persist_rom_metadata_cache(settings, cache_data)

            with mock.patch.object(RomRepository, "build_fingerprint", side_effect=AssertionError("unchanged ROM should not re-fingerprint")), \
                 mock.patch.object(RomRepository, "build_md5", side_effect=AssertionError("unchanged BIOS should not re-hash")):
                snapshot, changed, stats = _poll_rom_metadata_cache(settings, repo)
            self.assertFalse(changed)
            self.assertEqual(snapshot["roms"][0]["rom_fingerprint"], first_fingerprint)
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
            with mock.patch("app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", True):
                list(_hash_rom_metadata_batches(settings, repo, batch_size=1))
            _mark_rom_metadata_upload_clean(settings)
            gamelist.write_text(
                "<gameList><game><path>./Game.zip</path><name>Game</name>"
                "<image>./images/game.png</image><marquee>./images/game-marquee.png</marquee>"
                "</game></gameList>\n",
                encoding="utf-8",
            )

            with mock.patch.object(RomRepository, "build_fingerprint", side_effect=AssertionError("unchanged ROM should not hash")):
                snapshot, changed, stats = _poll_rom_metadata_cache(settings, repo)

            self.assertTrue(changed)
            self.assertTrue(stats["artwork_changed"])
            self.assertEqual(snapshot["artwork"][0]["artwork_types"], ["image", "marquee"])

    def test_rom_metadata_cache_reuses_fingerprint_when_only_mtime_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"rom-data")
            self._write_gamelist(rom.parent, "Game.zip")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)

            _poll_rom_metadata_cache(settings, repo)
            with mock.patch("app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", True):
                patches = list(_hash_rom_metadata_batches(settings, repo, batch_size=1))
            first_fingerprint = patches[0]["roms"][0]["rom_fingerprint"]
            cache_data, _ = _load_rom_metadata_cache(settings)
            cache_data["dirty"] = False
            _persist_rom_metadata_cache(settings, cache_data)

            os.utime(rom, (rom.stat().st_atime + 10, rom.stat().st_mtime + 10))

            with mock.patch.object(RomRepository, "build_fingerprint", side_effect=AssertionError("mtime-only ROM change should reuse cached fingerprint")):
                snapshot, changed, stats = _poll_rom_metadata_cache(settings, repo)
                patches = list(_hash_rom_metadata_batches(settings, repo, batch_size=1))

            self.assertTrue(changed)
            self.assertEqual(stats["new_or_changed"], 0)
            self.assertEqual(stats["roms_pending_fingerprint"], 0)
            self.assertEqual(snapshot["roms"][0]["rom_fingerprint"], first_fingerprint)
            self.assertEqual(patches, [])

    def test_corrupt_rom_metadata_cache_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"first")
            self._write_gamelist(rom.parent, "Game.zip")
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

    def test_transient_sqlite_open_error_preserves_metadata_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            db_path = _rom_metadata_cache_path(settings)
            db_path.parent.mkdir(parents=True)
            db_path.write_bytes(b"not touched")

            with mock.patch("app.rom_metadata_store._read_sqlite_rom_metadata_cache", side_effect=sqlite3.OperationalError("unable to open database file")):
                cache, missing = _load_rom_metadata_cache(settings)

            self.assertFalse(missing)
            self.assertTrue(db_path.exists())
            self.assertEqual(db_path.read_bytes(), b"not touched")
            self.assertEqual(cache["entries"], {})

    def test_legacy_json_rom_metadata_cache_migrates_to_incremental_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()
            legacy_path = root / "system" / "drone-app" / "rom_metadata_cache.json"
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "entries": {"snes:Game.zip": {"system": "snes", "file_path": "Game.zip", "rom_fingerprint": "abc"}},
                    "bios_entries": {},
                    "artwork_entries": {},
                    "systems": [{"name": "snes"}],
                    "gamelists": [],
                    "dirty": False,
                }),
                encoding="utf-8",
            )

            cache, rebuilt = _load_rom_metadata_cache(settings)
            reloaded, reloaded_rebuilt = _load_rom_metadata_cache(settings)

            self.assertFalse(rebuilt)
            self.assertFalse(reloaded_rebuilt)
            self.assertTrue(_rom_metadata_cache_path(settings).exists())
            self.assertEqual(cache["entries"]["snes:Game.zip"]["rom_fingerprint"], "abc")
            self.assertEqual(reloaded["entries"]["snes:Game.zip"]["file_path"], "Game.zip")

    def test_metadata_change_queue_uses_relational_rows_not_payload_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()
            cache = {
                "schema_version": 4,
                "dirty": True,
                "entries": {
                    "snes:Game.zip": {
                        "system": "snes",
                        "file_path": "Game.zip",
                        "rom_fingerprint": "abc",
                        "file_size": 3,
                        "modified_time": 10,
                        "gamelist_path": "/userdata/roms/snes/gamelist.xml",
                        "gamelist_game_id": "game-1",
                    },
                },
                "bios_entries": {},
                "artwork_entries": {},
            }

            _persist_rom_metadata_cache(settings, cache, rom_updates=cache["entries"])
            _persist_rom_metadata_cache(
                settings,
                {**cache, "entries": {}},
                rom_deletes=["snes:Game.zip"],
                rom_deleted_rows=cache["entries"],
            )
            changes = _read_pending_rom_metadata_changes(settings)

            with sqlite3.connect(_rom_metadata_cache_path(settings)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(cache_changes)")}
                tombstone = connection.execute(
                    "SELECT fingerprint, gamelist_path, gamelist_game_id FROM deleted_rom_cache_entries WHERE entry_key = ?",
                    ("snes:Game.zip",),
                ).fetchone()

            self.assertNotIn("payload", columns)
            self.assertEqual(tombstone, ("abc", "/userdata/roms/snes/gamelist.xml", "game-1"))
            self.assertEqual(changes["deleted"]["roms"][0]["rom_fingerprint"], "abc")
            self.assertNotIn("gamelist", changes["deleted"]["roms"][0])

    def test_legacy_payload_change_queue_migrates_to_relational_tombstones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()
            db_path = _rom_metadata_cache_path(settings)
            db_path.parent.mkdir(parents=True)
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "CREATE TABLE cache_changes (asset_type TEXT NOT NULL, entry_key TEXT NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (asset_type, entry_key))"
                )
                connection.execute(
                    "INSERT INTO cache_changes (asset_type, entry_key, operation, payload) VALUES (?, ?, ?, ?)",
                    (
                        "rom",
                        "snes:Removed.zip",
                        "delete",
                        json.dumps({
                            "system": "snes",
                            "file_path": "Removed.zip",
                            "rom_fingerprint": "kept-md5",
                            "gamelist_path": "/userdata/roms/snes/gamelist.xml",
                            "gamelist_game_id": "removed-game",
                            "gamelist": {"name": "Should not persist"},
                        }),
                    ),
                )

            changes = _read_pending_rom_metadata_changes(settings)

            with sqlite3.connect(db_path) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(cache_changes)")}
                legacy_table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'cache_changes_payload_legacy'"
                ).fetchone()

            self.assertNotIn("payload", columns)
            self.assertIsNone(legacy_table)
            self.assertEqual(changes["deleted"]["roms"][0]["rom_fingerprint"], "kept-md5")
            self.assertEqual(changes["deleted"]["roms"][0]["gamelist_game_id"], "removed-game")
            self.assertNotIn("gamelist", changes["deleted"]["roms"][0])

    def test_metadata_initialization_preserves_other_shared_database_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()
            save_payload(database_path(root), "credentials", {"username": "kept"})

            _poll_rom_metadata_cache(settings, RomRepository(root / "roms", root / "bios"))

            self.assertEqual(load_payload(database_path(root), "credentials", {})["username"], "kept")

    def test_rom_metadata_poll_does_not_write_monolithic_json_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"rom")
            self._write_gamelist(rom.parent, "Game.zip")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()

            snapshot, changed, _ = _poll_rom_metadata_cache(
                settings,
                RomRepository(settings.roms_root, settings.bios_root),
            )

            self.assertTrue(changed)
            self.assertEqual(len(snapshot["roms"]), 1)
            self.assertTrue(_rom_metadata_cache_path(settings).exists())

    def test_rom_metadata_poll_uses_gamelist_for_system_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"rom")
            (rom.parent / "Loose.zip").write_bytes(b"loose")
            self._write_gamelist(rom.parent, "Game.zip")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)

            with mock.patch.object(repo, "_count_rom_items", side_effect=AssertionError("poll must not pre-count ROMs")), mock.patch.object(
                repo, "_list_rom_items", side_effect=AssertionError("poll must use gamelist.xml")
            ):
                snapshot, _, _ = _poll_rom_metadata_cache(settings, repo)

            self.assertEqual(snapshot["systems"], [{"name": "snes", "rom_count": 1}])
            self.assertEqual([row["file_path"] for row in snapshot["roms"]], ["Game.zip"])

    def test_background_rom_hashing_can_be_disabled_for_responsive_ui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"rom")
            self._write_gamelist(rom.parent, "Game.zip")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)
            _poll_rom_metadata_cache(settings, repo)

            with mock.patch("app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", False), mock.patch.object(
                repo, "build_fingerprint", side_effect=AssertionError("ROM hashing should be disabled")
            ):
                self.assertEqual(list(_hash_rom_metadata_batches(settings, repo, batch_size=1)), [])

    def test_upload_clean_updates_sqlite_state_without_loading_asset_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()
            cache = {
                "schema_version": 2,
                "dirty": True,
                "entries": {"snes:game.zip": {"file_path": "Game.zip"}},
                "bios_entries": {},
                "artwork_entries": {},
            }
            _persist_rom_metadata_cache(settings, cache, rom_updates=cache["entries"])

            with mock.patch("app.drone_api._load_rom_metadata_cache", side_effect=AssertionError("must not decode all cache rows")):
                _mark_rom_metadata_upload_clean(settings)

            updated, _ = _load_rom_metadata_cache(settings)
            self.assertFalse(updated["dirty"])
            self.assertTrue(updated["last_successful_upload_at"])
            self.assertIn("snes:game.zip", updated["entries"])

    def test_rom_metadata_scan_checkpoint_survives_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "First.zip").write_bytes(b"first")
            (system / "Second.zip").write_bytes(b"second")
            self._write_gamelist(system, "First.zip", "Second.zip")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)
            interrupted = False

            def interrupt_after_checkpoint(cache_settings, payload, **kwargs):
                nonlocal interrupted
                _persist_rom_metadata_cache(cache_settings, payload, **kwargs)
                if payload.get("scan_in_progress") and not interrupted:
                    interrupted = True
                    raise RuntimeError("simulated reset")

            with mock.patch("app.drone_api.ROM_METADATA_PROGRESS_FILES", 1), mock.patch(
                "app.drone_api._persist_rom_metadata_cache", side_effect=interrupt_after_checkpoint
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated reset"):
                    _poll_rom_metadata_cache(settings, repo)

            partial, _ = _load_rom_metadata_cache(settings)
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
            original_md5 = RomRepository.build_md5  # BIOS uses a full-file MD5, not the sampled fingerprint

            def interrupted_hash(path, **kwargs):
                if path.name == "B.bin":
                    raise RuntimeError("simulated reset")
                return original_md5(path)

            with mock.patch("app.drone_api.ROM_METADATA_PROGRESS_FILES", 1), mock.patch.object(
                RomRepository, "build_md5", side_effect=interrupted_hash
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated reset"):
                    _poll_rom_metadata_cache(settings, repo)

            partial, _ = _load_rom_metadata_cache(settings)
            self.assertTrue(partial["bios_entries"]["a.bin"]["bios_md5"])
            self.assertNotIn("bios_md5", partial["bios_entries"]["b.bin"])

            hashed_after_restart = []

            def track_hash(path, **kwargs):
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
            self._write_gamelist(system, "A.zip", "B.zip")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)
            _poll_rom_metadata_cache(settings, repo)
            original_fingerprint = RomRepository.build_fingerprint

            def interrupted_hash(path, **kwargs):
                if path.name == "B.zip":
                    raise RuntimeError("simulated reset")
                return original_fingerprint(path)

            with mock.patch("app.drone_api.ROM_METADATA_PROGRESS_FILES", 1), mock.patch(
                "app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", True
            ), mock.patch.object(repo, "build_fingerprint", side_effect=interrupted_hash):
                with self.assertRaisesRegex(RuntimeError, "simulated reset"):
                    list(_hash_rom_metadata_batches(settings, repo, batch_size=1000))

            partial, _ = _load_rom_metadata_cache(settings)
            self.assertTrue(partial["entries"]["snes:A.zip"]["rom_fingerprint"])
            self.assertNotIn("rom_fingerprint", partial["entries"]["snes:B.zip"])

            hashed_after_restart = []

            def track_hash(path, **kwargs):
                hashed_after_restart.append(path.name)
                return original_fingerprint(path)

            with mock.patch("app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", True), mock.patch.object(repo, "build_fingerprint", side_effect=track_hash):
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
            self._write_gamelist(rom.parent, "Game.zip")
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
            with mock.patch("app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", True):
                list(_hash_rom_metadata_batches(settings, repo, batch_size=1))
            _mark_rom_metadata_upload_clean(settings)

            uploads = []

            def fake_post(url, payload, token=None, settings=None, timeout_seconds=10):
                uploads.append((url, payload, token))
                return 200, {
                    "rom_count": len(payload.get("roms") or []),
                    "bios_count": len(payload.get("bios") or []),
                    "artwork_count": len(payload.get("artwork") or []),
                }

            with mock.patch.object(RomRepository, "build_fingerprint", side_effect=AssertionError("unchanged ROM should not hash")), mock.patch(
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
            cache_after, _ = _load_rom_metadata_cache(settings)
            self.assertFalse(cache_after["dirty"])
            self.assertTrue(cache_after["last_successful_upload_at"])

    def test_rom_metadata_sync_force_uploads_clean_database_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Game.zip").write_bytes(b"rom")
            self._write_gamelist(system, "Game.zip")
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
            with mock.patch("app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", True):
                list(_hash_rom_metadata_batches(settings, repo, batch_size=1))
            cache_data, _ = _load_rom_metadata_cache(settings)
            cache_data["dirty"] = False
            _persist_rom_metadata_cache(settings, cache_data)
            uploads = []

            def fake_post(url, payload, token=None, settings=None, timeout_seconds=10):
                uploads.append(payload)
                return 200, {
                    "rom_count": len(payload.get("roms") or []),
                    "bios_count": len(payload.get("bios") or []),
                    "artwork_count": len(payload.get("artwork") or []),
                }

            with mock.patch.object(RomRepository, "build_fingerprint", side_effect=AssertionError("force inventory should not rehash clean ROM")), mock.patch(
                "app.drone_api._overmind_post_json_with_status", side_effect=fake_post
            ):
                result = _sync_rom_metadata_to_overmind(
                    settings,
                    repo,
                    {"overmind_token": "drone-token"},
                    "https://overmind.local",
                    "drone-token",
                    force_upload=True,
                )

            self.assertEqual(result["status"], "uploaded")
            self.assertTrue(result["forced"])
            self.assertEqual(len(uploads), 1)
            self.assertEqual(uploads[0]["update_mode"], "inventory")
            self.assertTrue(uploads[0]["replace_all"])
            self.assertEqual(len(uploads[0]["roms"]), 1)

    def test_rom_metadata_sync_full_refreshes_clean_cache_without_successful_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Game.zip").write_bytes(b"rom")
            self._write_gamelist(system, "Game.zip")
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
            with mock.patch("app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", True):
                list(_hash_rom_metadata_batches(settings, repo, batch_size=1))
            cache_data, _ = _load_rom_metadata_cache(settings)
            cache_data["dirty"] = False
            cache_data["last_successful_upload_at"] = None
            _persist_rom_metadata_cache(settings, cache_data)
            uploads = []

            def fake_post(url, payload, token=None, settings=None, timeout_seconds=10):
                uploads.append(payload)
                return 200, {
                    "rom_count": len(payload.get("roms") or []),
                    "bios_count": len(payload.get("bios") or []),
                    "artwork_count": len(payload.get("artwork") or []),
                }

            with mock.patch.object(RomRepository, "build_fingerprint", side_effect=AssertionError("clean cache should not rehash")), mock.patch(
                "app.drone_api._overmind_post_json_with_status", side_effect=fake_post
            ):
                result = _sync_rom_metadata_to_overmind(
                    settings,
                    repo,
                    {"overmind_token": "drone-token"},
                    "https://overmind.local",
                    "drone-token",
                )

            self.assertEqual(result["status"], "uploaded")
            self.assertFalse(result["forced"])
            self.assertEqual(len(uploads), 1)
            self.assertEqual(uploads[0]["update_mode"], "inventory")
            self.assertTrue(uploads[0]["replace_all"])
            self.assertEqual(len(uploads[0]["roms"]), 1)
            cache_after, _ = _load_rom_metadata_cache(settings)
            self.assertTrue(cache_after["last_successful_upload_at"])

    def test_rom_metadata_cache_status_reports_progress_and_pending_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Game.zip").write_bytes(b"rom")
            self._write_gamelist(system, "Game.zip")
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "OVERMIND_DEVICE_ID": "drone-a",
                    "ROM_METADATA_POLL_SECONDS": "300",
                },
                clear=True,
            ):
                settings = Settings.from_env()
            _poll_rom_metadata_cache(settings, RomRepository(settings.roms_root, settings.bios_root))

            status = _rom_metadata_cache_status(settings)

            self.assertTrue(status["complete"])
            self.assertFalse(status["uploaded"])
            self.assertTrue(status["needs_upload"])
            self.assertEqual(status["counts"]["roms"], 1)
            self.assertEqual(status["counts"]["systems"], 1)
            self.assertGreaterEqual(status["pending_changes"]["total"], 1)
            self.assertIn("rom_metadata_cache.sqlite3", status["path"])

            drone_api._clear_pending_rom_metadata_changes(settings)
            drone_api._update_rom_metadata_cache_state(settings, dirty=False, full_refresh_pending=False)
            cleared_status = _rom_metadata_cache_status(settings)

            self.assertEqual(cleared_status["pending_changes"]["total"], 0)
            self.assertFalse(cleared_status["needs_upload"])
            self.assertEqual(cleared_status["counts"]["roms"], 1)

    def test_collect_rom_metadata_uses_database_cache_with_current_gamelist_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Game.zip").write_bytes(b"rom")
            (system / "gamelist.xml").write_text(
                "<gameList><game><path>./Game.zip</path><name>Cached Title</name></game></gameList>\n",
                encoding="utf-8",
            )
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
            (system / "gamelist.xml").write_text(
                "<gameList><game><path>./Game.zip</path><name>Stale XML Title</name></game></gameList>\n",
                encoding="utf-8",
            )

            with mock.patch.object(repo, "list_assets", side_effect=AssertionError("collect should use local database cache")):
                result = _collect_rom_metadata(settings, repo)

            self.assertEqual(result["type"], "asset_metadata")
            self.assertEqual(result["roms"][0]["rom_name"], "Stale XML Title")
            self.assertEqual(result["roms"][0]["gamelist_path"], str((system / "gamelist.xml").resolve()))
            self.assertEqual(result["roms"][0]["gamelist_game_id"], "Game.zip")
            self.assertEqual(result["gamelists"][0]["rom_count"], 1)

    def test_rom_metadata_sync_uploads_inventory_then_batched_fingerprint_patches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Game One.zip").write_bytes(b"one")
            (system / "Game Two.zip").write_bytes(b"two")
            self._write_gamelist(system, "Game One.zip", "Game Two.zip")
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

            def fake_post(url, payload, token=None, settings=None, timeout_seconds=10):
                uploads.append(payload)
                return 200, {
                    "rom_count": len(payload.get("roms") or []),
                    "bios_count": len(payload.get("bios") or []),
                    "artwork_count": len(payload.get("artwork") or []),
                }

            with mock.patch("app.drone_api.ROM_METADATA_FINGERPRINT_BATCH_SIZE", 1), mock.patch(
                "app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", True
            ), mock.patch(
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
            self.assertTrue(uploads[0]["replace_all"])
            self.assertTrue(all("rom_fingerprint" not in row for row in uploads[0]["roms"]))
            self.assertTrue(all(len(payload["roms"]) == 1 and payload["roms"][0].get("rom_fingerprint") for payload in uploads[1:]))

    def test_rom_metadata_sync_flags_full_refresh_when_hash_patch_upload_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Game One.zip").write_bytes(b"one")
            self._write_gamelist(system, "Game One.zip")
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

            def fake_post(url, payload, token=None, settings=None, timeout_seconds=10):
                if payload.get("update_mode") == "rom_hash_patch":
                    raise RuntimeError("overmind unavailable")
                return 200, {
                    "rom_count": len(payload.get("roms") or []),
                    "bios_count": len(payload.get("bios") or []),
                    "artwork_count": len(payload.get("artwork") or []),
                }

            with mock.patch("app.drone_api.ROM_METADATA_FINGERPRINT_BATCH_SIZE", 1), mock.patch(
                "app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", True
            ), mock.patch(
                "app.drone_api._overmind_post_json_with_status", side_effect=fake_post
            ):
                # The failed hash patch must not abort the poll; it should flag a
                # full refresh so the next poll resends md5 instead of losing it.
                _sync_rom_metadata_to_overmind(
                    settings,
                    repo,
                    {"overmind_token": "drone-token"},
                    "https://overmind.local",
                    "drone-token",
                )

            state = drone_api._read_rom_metadata_cache_state(
                settings, "full_refresh_pending", "dirty"
            )
            self.assertTrue(state.get("full_refresh_pending"))
            self.assertTrue(state.get("dirty"))

    def test_rom_metadata_sync_readvertises_true_fingerprint_when_state_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Game One.zip").write_bytes(b"one")
            self._write_gamelist(system, "Game One.zip")
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

            def fake_post(url, payload, token=None, settings=None, timeout_seconds=10):
                return 200, {
                    "rom_count": len(payload.get("roms") or []),
                    "bios_count": len(payload.get("bios") or []),
                    "artwork_count": len(payload.get("artwork") or []),
                }

            with mock.patch("app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", True), mock.patch(
                "app.drone_api._overmind_post_json_with_status", side_effect=fake_post
            ):
                # First sync hashes md5 and records the true fingerprint.
                _sync_rom_metadata_to_overmind(
                    settings, repo, {}, "https://overmind.local", "drone-token"
                )
                # Simulate a drone whose md5 never reached Overmind: its stored
                # fingerprint still reflects the md5-less inventory.
                drone_api._update_rom_metadata_cache_state(
                    settings, rom_inventory_fingerprint="stale-md5-less"
                )
                # Nothing changed on disk -> the poll takes the no-changes path,
                # which must re-advertise the real (md5-bearing) fingerprint so
                # Overmind detects the drift and resyncs on its own.
                result = _sync_rom_metadata_to_overmind(
                    settings, repo, {}, "https://overmind.local", "drone-token"
                )

            self.assertEqual(result["status"], "skipped")
            fingerprint = drone_api._read_rom_metadata_cache_state(
                settings, "rom_inventory_fingerprint"
            ).get("rom_inventory_fingerprint")
            self.assertTrue(fingerprint)
            self.assertNotEqual(fingerprint, "stale-md5-less")

    def test_rom_metadata_sync_persists_and_uploads_added_and_deleted_roms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            first = system / "First Game.zip"
            second = system / "Second Game.zip"
            first.write_bytes(b"one")
            self._write_gamelist(system, "First Game.zip")
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

            def fake_post(url, payload, token=None, settings=None, timeout_seconds=10):
                uploads.append(payload)
                return 200, {
                    "rom_count": len(payload.get("roms") or []),
                    "bios_count": len(payload.get("bios") or []),
                    "artwork_count": len(payload.get("artwork") or []),
                }

            with mock.patch("app.drone_api._overmind_post_json_with_status", side_effect=fake_post):
                _sync_rom_metadata_to_overmind(settings, repo, {}, "https://overmind.local", "drone-token")
                second.write_bytes(b"two")
                self._write_gamelist(system, "First Game.zip", "Second Game.zip")
                added = _sync_rom_metadata_to_overmind(settings, repo, {}, "https://overmind.local", "drone-token")
                first.unlink()
                self._write_gamelist(system, "Second Game.zip")
                deleted = _sync_rom_metadata_to_overmind(settings, repo, {}, "https://overmind.local", "drone-token")

            inventories = [payload for payload in uploads if payload.get("update_mode") == "inventory_delta"]
            full_refreshes = [payload for payload in uploads if payload.get("update_mode") == "inventory"]
            self.assertEqual(len(full_refreshes), 1)
            self.assertTrue(full_refreshes[0]["replace_all"])
            self.assertEqual([len(payload["roms"]) for payload in inventories], [1, 0])
            self.assertEqual(inventories[-1]["deleted"]["roms"][0]["file_path"], "First Game.zip")
            self.assertEqual(added["stats"]["new_or_changed"], 1)
            self.assertEqual(deleted["stats"]["deleted"], 1)
            cache, _ = _load_rom_metadata_cache(settings)
            self.assertEqual(len(cache["entries"]), 1)
            self.assertEqual(next(iter(cache["entries"].values()))["file_path"], "Second Game.zip")
            self.assertFalse(cache["dirty"])

    def test_rom_metadata_delta_upload_chunks_and_retries_until_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Game One.zip").write_bytes(b"one")
            (system / "Game Two.zip").write_bytes(b"two")
            self._write_gamelist(system, "Game One.zip", "Game Two.zip")
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
            cache, _ = _load_rom_metadata_cache(settings)
            cache["last_successful_upload_at"] = "2026-05-30T00:00:00+00:00"
            _persist_rom_metadata_cache(settings, cache)
            uploads = []

            def failing_post(url, payload, token=None, settings=None, timeout_seconds=10):
                uploads.append(payload)
                if payload.get("update_mode") == "inventory_delta" and payload.get("delta_index") == 1:
                    raise URLError("temporary outage")
                return 200, {
                    "rom_count": len(payload.get("roms") or []),
                    "bios_count": len(payload.get("bios") or []),
                    "artwork_count": len(payload.get("artwork") or []),
                }

            with mock.patch("app.drone_api.ROM_METADATA_UPLOAD_CHUNK_SIZE", 1), mock.patch(
                "app.drone_api._overmind_post_json_with_status", side_effect=failing_post
            ):
                with self.assertRaises(URLError):
                    _sync_rom_metadata_to_overmind(settings, repo, {}, "https://overmind.local", "drone-token")

            cache, _ = _load_rom_metadata_cache(settings)
            self.assertTrue(cache["dirty"])
            self.assertEqual([payload.get("delta_index") for payload in uploads if payload.get("update_mode") == "inventory_delta"], [0, 1])

            uploads.clear()

            def successful_post(url, payload, token=None, settings=None, timeout_seconds=10):
                uploads.append(payload)
                return 200, {
                    "rom_count": len(payload.get("roms") or []),
                    "bios_count": len(payload.get("bios") or []),
                    "artwork_count": len(payload.get("artwork") or []),
                }

            with mock.patch("app.drone_api.ROM_METADATA_UPLOAD_CHUNK_SIZE", 1), mock.patch(
                "app.drone_api._overmind_post_json_with_status", side_effect=successful_post
            ):
                result = _sync_rom_metadata_to_overmind(settings, repo, {}, "https://overmind.local", "drone-token")

            cache, _ = _load_rom_metadata_cache(settings)
            self.assertFalse(cache["dirty"])
            self.assertEqual(result["status"], "uploaded")
            self.assertEqual([payload.get("delta_index") for payload in uploads if payload.get("update_mode") == "inventory_delta"], [0, 1])
            self.assertTrue(all(item.get("payload_bytes", 0) > 0 for item in result["uploads"]))

    def test_rom_metadata_poll_hashes_roms_by_default_when_cached_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"offline-rom")
            self._write_gamelist(rom.parent, "Game.zip")
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
            cache, _ = _load_rom_metadata_cache(settings)
            self.assertTrue(cache["dirty"])
            self.assertIn("rom_fingerprint", next(iter(cache["entries"].values())))

    def test_rom_metadata_poll_does_not_register_without_auth_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"offline-rom")
            self._write_gamelist(rom.parent, "Game.zip")
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "OVERMIND_URL": "https://overmind.local",
                    "OVERMIND_DEVICE_ID": "drone-a",
                },
                clear=True,
            ):
                settings = Settings.from_env()

            config = {
                "overmind_url": "https://overmind.local",
                "overmind_token": "",
                "overmind_auth_token": "",
                "integration_enabled": False,
            }
            with mock.patch("app.drone_api._load_overmind_config_for_settings", return_value=config):
                with mock.patch(
                    "app.drone_api._register_or_claim_overmind_token",
                    side_effect=AssertionError("must not register without approved auth token"),
                ):
                    result = _poll_rom_metadata_once(settings, RomRepository(settings.roms_root, settings.bios_root))

            self.assertEqual(result["status"], "cached")
            self.assertEqual(result["reason"], "overmind_not_connected")
            cache, _ = _load_rom_metadata_cache(settings)
            self.assertTrue(cache["dirty"])

    def test_rom_metadata_poll_defers_hashing_when_overmind_upload_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            rom = root / "roms" / "snes" / "Game.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"offline-rom")
            self._write_gamelist(rom.parent, "Game.zip")
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

            cache, _ = _load_rom_metadata_cache(settings)
            self.assertTrue(cache["dirty"])
            self.assertNotIn("rom_fingerprint", next(iter(cache["entries"].values())))

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

            with mock.patch.object(RomRepository, "build_fingerprint", side_effect=AssertionError("should not hash")):
                systems = repo.list_systems()

            self.assertEqual(systems, [{"name": "snes", "rom_count": 1}])

    def test_list_systems_uses_sqlite_system_counts_without_loading_rom_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            (root / "roms" / "snes").mkdir(parents=True)
            (root / "bios").mkdir(parents=True)
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()
            cache = {
                "schema_version": 4,
                "last_full_scan_at": "2026-06-01T00:00:00+00:00",
                "entries": {"snes:game.zip": {"system": "snes", "file_path": "game.zip", "rom_name": "Game"}},
                "bios_entries": {},
                "artwork_entries": {},
                "systems": [{"name": "snes", "rom_count": 1}],
                "gamelists": [],
            }
            _persist_rom_metadata_cache(settings, cache, rom_updates=cache["entries"])
            repo = RomRepository(root / "roms", root / "bios")

            with mock.patch("app.drone_api._load_rom_metadata_cache", side_effect=AssertionError("must not decode all cache rows")):
                systems = repo.list_systems()

            self.assertEqual(systems, [{"name": "snes", "rom_count": 1}])

    def test_search_roms_from_mock_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            repo = RomRepository(root / "roms", root / "bios")
            results = repo.search_roms("mario")
            self.assertTrue(any(item["name"].lower().startswith("mario") for item in results))

    def test_search_roms_uses_relational_cache(self) -> None:
        # With settings + a populated SQLite cache (and an empty filesystem), search must
        # come from the relational cache via the FTS/LIKE SQL path, not a filesystem scan.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            entries = {
                "snes:smw.zip": {"system": "snes", "file_path": "smw.zip", "rom_name": "Super Mario World", "unique_id": "u1"},
                "gba:metroid.zip": {"system": "gba", "file_path": "metroid.zip", "rom_name": "Metroid Fusion", "unique_id": "u2"},
            }
            _persist_rom_metadata_cache(
                settings,
                {
                    **_empty_rom_metadata_cache(),
                    "entries": entries,
                    "systems": [{"name": "snes", "rom_count": 1}, {"name": "gba", "rom_count": 1}],
                    "dirty": False,
                    "full_refresh_pending": False,
                },
                rom_updates=entries,
            )
            repo = RomRepository(settings.roms_root, settings.bios_root, settings=settings)

            # Mid-word substring match (the filesystem is empty, so a result proves the cache path).
            names = {item["name"] for item in repo.search_roms("etroid")}
            self.assertIn("Metroid Fusion", names)
            # System filter is applied in SQL.
            snes = repo.search_roms("mario", system_filter="snes")
            self.assertTrue(snes)
            self.assertTrue(all(item["system"] == "snes" for item in snes))
            self.assertTrue(any(item["name"] == "Super Mario World" for item in snes))
            # Empty query returns nothing.
            self.assertEqual(repo.search_roms(""), [])

    def test_list_assets_roms_uses_relational_cache_by_system(self) -> None:
        # list_assets(roms) must query just the requested system from SQLite, not load
        # the whole library. Empty filesystem + populated/ready cache proves the SQL path.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            entries = {
                "snes:b.zip": {"system": "snes", "file_path": "b.zip", "rom_name": "Beta", "unique_id": "u-b"},
                "snes:a.zip": {"system": "snes", "file_path": "a.zip", "rom_name": "Alpha", "unique_id": "u-a"},
                "gba:m.zip": {"system": "gba", "file_path": "m.zip", "rom_name": "Metroid", "unique_id": "u-m"},
            }
            _persist_rom_metadata_cache(
                settings,
                {
                    **_empty_rom_metadata_cache(),
                    "entries": entries,
                    "systems": [{"name": "snes", "rom_count": 2}, {"name": "gba", "rom_count": 1}],
                    "dirty": False,
                    "full_refresh_pending": False,
                    "last_full_scan_at": "2026-06-08T00:00:00Z",
                    "scan_in_progress": False,
                },
                rom_updates=entries,
            )
            # System dirs exist (as in production) but hold no rom files, so any result
            # must come from the cache, not a filesystem listing.
            (root / "roms" / "snes").mkdir(parents=True)
            (root / "roms" / "gba").mkdir(parents=True)
            repo = RomRepository(settings.roms_root, settings.bios_root, settings=settings)

            _, roms = repo.list_assets("snes", "roms", include_fingerprint=False)
            names = [item["name"] for item in roms]
            self.assertEqual(names, ["Alpha", "Beta"])  # ordered, only snes
            self.assertTrue(all(item["unique_id"] for item in roms))
            self.assertEqual(len(repo.list_assets("gba", "roms")[1]), 1)

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

    def test_update_gamelist_artwork_reference_creates_rom_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "fbneo"
            (system / "images").mkdir(parents=True)
            (system / "1942.zip").write_bytes(b"rom")
            (system / "images" / "1942.png").write_bytes(b"image")
            repo = RomRepository(root / "roms", root / "bios")

            result = repo.update_gamelist_artwork_reference("fbneo", "1942.zip", "image", "images/1942.png")

            self.assertEqual(result["rom_path"], "1942.zip")
            self.assertEqual(result["artwork_path"], "images/1942.png")
            text = (system / "gamelist.xml").read_text(encoding="utf-8")
            self.assertIn("<path>./1942.zip</path>", text)
            self.assertIn("<name>1942</name>", text)
            self.assertIn("<image>./images/1942.png</image>", text)


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
