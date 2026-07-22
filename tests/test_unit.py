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
import time
import unittest
from threading import Event
from unittest import mock
from pathlib import Path
from urllib.error import URLError

import app.drone_api as drone_api
from app.transfer import local_network
from app.mock_data import seed_mock_userdata
from app.storage.state_store import (
    database_path,
    database_path_for_legacy_file,
    load_payload,
    load_peer_route,
    open_database,
    save_payload,
    save_peer_route,
)
from app.device.emulator_configs import (
    list_emulator_config_files,
    read_emulator_config_file,
)
from app.drone_api import (
    BasicAuth,
    DownloadCancelled,
    DownloadManager,
    DroneCredentialStore,
    DroneThreadingHTTPServer,
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
    _get_local_ip_addresses,
    _get_router_ip_address,
    _collect_gpu_info,
    _launchbox_platform_for_system,
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
    _sample_speed,
    _real_data_roots,
    _peer_ssl_diagnostic,
    _peer_trust_cafile,
    _download_rom_from_peer,
    _download_rom_folder_from_peer,
    _collision_safe_target,
    _rom_fingerprint_exists,
    _collect_mounted_disk_metrics,
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
        with mock.patch("app.common.auth.DRONE_UNAUTH_RATE_LIMIT_REQUESTS", 2), mock.patch(
            "app.common.auth.DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS", 10
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

    def test_local_network_mode_cannot_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "local-a"}, clear=True):
                settings = Settings.from_env()
            self.assertTrue(local_network.is_local_mode(settings))
            self.assertEqual(local_network.get_mode(settings), local_network.MODE_LOCAL_NETWORK)
            with self.assertRaises(ValueError):
                local_network.set_mode(settings, "disabled")
            with self.assertRaises(ValueError):
                local_network.set_integrations(settings, local_network_enabled=False)
            # Still on after the rejected attempts.
            self.assertTrue(local_network.is_local_mode(settings))

    def test_discovery_and_pairing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "local-a"}, clear=True):
                settings = Settings.from_env()
                announcement = {
                    "service": local_network.DISCOVERY_SERVICE,
                    "drone_id": "local-b",
                    "name": "Cabinet B",
                    "scheme": "https",
                    "api_port": 443,
                }
                discovered = local_network.record_discovered_peer(settings, announcement, "192.168.1.22")
                self.assertEqual(discovered["drone_id"], "local-b")
                self.assertFalse(discovered["paired"])
                paired = local_network.save_paired_peer(settings, {**discovered, "certificate_fingerprint": "abc"})
                self.assertTrue(paired["paired"])
                self.assertEqual(local_network.get_paired_peer(settings, "local-b")["certificate_fingerprint"], "abc")

    def test_local_ip_addresses_and_cert_ips_include_tailnet(self) -> None:
        from app.transfer import network_identity

        with mock.patch.object(network_identity, "get_tailnet_ip", return_value="100.101.1.2"):
            network = network_identity.get_local_ip_addresses()
            cert_ips = network_identity.get_local_certificate_ips()
        self.assertEqual(network["tailnet_ip"], "100.101.1.2")
        self.assertNotIn("100.101.1.2", network["ipv4"])  # host/report selection unchanged
        self.assertIn("100.101.1.2", cert_ips)

        with mock.patch.object(network_identity, "get_tailnet_ip", return_value=None):
            network = network_identity.get_local_ip_addresses()
            cert_ips = network_identity.get_local_certificate_ips()
        self.assertIsNone(network["tailnet_ip"])
        self.assertFalse(any(ip.startswith("100.101.") for ip in cert_ips))

    def test_discovery_payload_and_record_carry_tailnet_ip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "local-a"}, clear=True):
                settings = Settings.from_env()
                local_network.set_mode(settings, local_network.MODE_LOCAL_NETWORK)
                with mock.patch.object(local_network, "get_tailnet_ip", return_value="100.64.0.5"):
                    payload = local_network.discovery_payload(settings, "aa:bb")
                self.assertEqual(payload["tailnet_ip"], "100.64.0.5")
                with mock.patch.object(local_network, "get_tailnet_ip", return_value=None):
                    self.assertEqual(local_network.discovery_payload(settings, "aa:bb")["tailnet_ip"], "")

                announcement = {
                    "service": local_network.DISCOVERY_SERVICE,
                    "drone_id": "local-b",
                    "name": "Cabinet B",
                    "scheme": "https",
                    "api_port": 443,
                    "tailnet_ip": "100.64.0.9",
                }
                discovered = local_network.record_discovered_peer(settings, announcement, "192.168.1.22")
                self.assertEqual(discovered["tailnet_ip"], "100.64.0.9")
                # An announce without the key (older peer version) keeps the stored value...
                legacy = {key: value for key, value in announcement.items() if key != "tailnet_ip"}
                self.assertEqual(local_network.record_discovered_peer(settings, legacy, "192.168.1.22")["tailnet_ip"], "100.64.0.9")
                # A temporary empty announce keeps the stable route so a peer
                # can still be reached after it moves off this LAN.
                preserved = local_network.record_discovered_peer(
                    settings,
                    {**announcement, "tailnet_ip": ""},
                    "192.168.1.22",
                )
                self.assertEqual(preserved["tailnet_ip"], "100.64.0.9")

    def test_tailnet_announce_refreshes_paired_peer_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "local-a"}, clear=True):
                settings = Settings.from_env()
                local_network.set_mode(settings, local_network.MODE_LOCAL_NETWORK)
                local_network.save_paired_peer(settings, {"drone_id": "local-b", "certificate_fingerprint": "abc"})
                local_network.record_discovered_peer(
                    settings,
                    {
                        "service": local_network.DISCOVERY_SERVICE,
                        "drone_id": "local-b",
                        "name": "Cabinet B",
                        "scheme": "https",
                        "api_port": 443,
                        "tailnet_ip": "100.64.0.9",
                    },
                    "192.168.1.22",
                )
                self.assertEqual(local_network.get_paired_peer(settings, "local-b")["tailnet_ip"], "100.64.0.9")

    def test_forgetting_tailnet_peer_suppresses_automatic_repair_until_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "local-a"}, clear=True):
                settings = Settings.from_env()
                local_network.save_paired_peer(
                    settings,
                    {"drone_id": "local-b", "tailnet_ip": "100.64.0.9"},
                )
                save_peer_route(
                    database_path(settings.userdata_root),
                    "local-b",
                    "https://100.64.0.9",
                    "tailnet",
                )
                self.assertTrue(local_network.forget_peer(settings, "local-b"))
                self.assertIsNone(load_peer_route(database_path(settings.userdata_root), "local-b"))
                self.assertTrue(local_network.is_tailnet_peer_forgotten(settings, "local-b"))
                local_network.save_paired_peer(
                    settings,
                    {"drone_id": "local-b", "pairing_source": "tailnet", "tailnet_ip": "100.64.0.9"},
                )
                self.assertFalse(local_network.is_tailnet_peer_forgotten(settings, "local-b"))

    def test_normalize_peer_address_accepts_bare_hosts_and_urls(self) -> None:
        from app.transfer.peer_connectivity import _normalize_peer_address

        self.assertEqual(_normalize_peer_address("100.64.0.7"), "https://100.64.0.7")
        self.assertEqual(_normalize_peer_address("100.64.0.7:8443"), "https://100.64.0.7:8443")
        self.assertEqual(_normalize_peer_address("https://100.64.0.7:443/"), "https://100.64.0.7")
        self.assertEqual(_normalize_peer_address("http://drone-den.local:80"), "http://drone-den.local")
        self.assertEqual(_normalize_peer_address("fd7a:115c:a1e0::1"), "https://[fd7a:115c:a1e0::1]")
        for bad in ("", "ftp://host", "https://", "host:notaport"):
            with self.assertRaises(ValueError):
                _normalize_peer_address(bad)

    def test_fetch_peer_info_validates_service_and_dials_info_endpoint(self) -> None:
        from app.transfer import peer_connectivity

        info = {
            "service": local_network.DISCOVERY_SERVICE,
            "drone_id": "local-b",
            "name": "Cabinet B",
            "scheme": "https",
            "api_port": 443,
            "tailnet_ip": "100.64.0.9",
            "certificate_fingerprint": "aa:bb",
        }
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(info).encode("utf-8")
        with mock.patch.object(peer_connectivity, "urlopen", return_value=response) as opened:
            fetched = peer_connectivity._fetch_peer_info("100.64.0.9")
        self.assertEqual(fetched["drone_id"], "local-b")
        self.assertEqual(opened.call_args[0][0].full_url, "https://100.64.0.9/v1/api/peer/info")

        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"service": "not-a-drone"}).encode("utf-8")
        with mock.patch.object(peer_connectivity, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "did not answer as a Batocera Drone"):
                peer_connectivity._fetch_peer_info("100.64.0.9")

    def test_local_pair_peer_exchanges_tailnet_addresses(self) -> None:
        from app.transfer import peer_connectivity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "local-a"}, clear=True):
                settings = Settings.from_env()
                local_network.set_mode(settings, local_network.MODE_LOCAL_NETWORK)
                pair_response = {
                    "status": "paired",
                    "drone_id": "local-b",
                    "name": "Cabinet B",
                    "scheme": "https",
                    "api_port": 443,
                    "reachable_url": "https://cabinet-b.local",
                    "tailnet_ip": "100.64.0.9",
                    "certificate_pem": "-----BEGIN CERTIFICATE-----fake",
                    "certificate_fingerprint": "aa:bb",
                }
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps(pair_response).encode("utf-8")
                fake_manager = mock.MagicMock()
                fake_manager.return_value.ensure_certificate.return_value = {
                    "public_certificate": "-----BEGIN CERTIFICATE-----own",
                    "fingerprint": "cc:dd",
                }
                cert_path = root / "peer-certs" / "local-b.pem"
                cert_path.parent.mkdir(parents=True, exist_ok=True)
                with mock.patch.object(peer_connectivity, "urlopen", return_value=response) as opened, \
                        mock.patch.object(peer_connectivity, "DroneCertificateManager", fake_manager), \
                        mock.patch.object(peer_connectivity, "_save_local_peer_certificate", return_value=(cert_path, "aa:bb")), \
                        mock.patch.object(local_network, "get_tailnet_ip", return_value="100.64.0.2"):
                    stored = peer_connectivity._local_pair_peer(
                        settings,
                        {"drone_id": "local-b", "reachable_url": "https://100.64.0.9", "tailnet_ip": "100.64.0.9"},
                        "",
                        tailnet_auto_pair=True,
                    )
                sent = json.loads(opened.call_args[0][0].data.decode("utf-8"))
                self.assertEqual(sent["tailnet_ip"], "100.64.0.2")  # our side advertised
                self.assertTrue(sent["tailnet_auto_pair"])
                self.assertEqual(stored["tailnet_ip"], "100.64.0.9")  # their side recorded
                self.assertEqual(stored["reachable_url"], "https://100.64.0.9")  # the dialed address sticks
                self.assertEqual(stored["pairing_source"], "tailnet")

    def test_discovery_surfaces_same_id_conflicts_from_other_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "local-a"}, clear=True):
                settings = Settings.from_env()
                local_network.set_mode(settings, local_network.MODE_LOCAL_NETWORK)
                with mock.patch.object(local_network, "_local_ipv4_addresses", return_value=["192.168.1.10"]):
                    discovered = local_network.record_discovered_peer(
                        settings,
                        {
                            "service": local_network.DISCOVERY_SERVICE,
                            "drone_id": "local-a",
                            "name": "Cloned Cabinet",
                            "scheme": "https",
                            "api_port": 443,
                        },
                        "192.168.1.22",
                    )
                self.assertIsNotNone(discovered)
                self.assertTrue(discovered["identity_conflict"])
                self.assertEqual(discovered["conflicting_drone_id"], "local-a")
                self.assertFalse(discovered["paired"])

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

        with mock.patch("app.transfer.drone_network._get_local_ip_addresses", return_value={"ipv4": ["172.20.0.10"], "ipv6": []}):
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

    def test_device_id_persists_after_first_physical_mac_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            device_id_file = root / "system" / "drone-app" / "device-id"
            device_id_file.parent.mkdir(parents=True)
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True), mock.patch(
                "app.common.device_identity._physical_mac_candidates",
                return_value=["58:47:ca:7e:38:57"],
            ), mock.patch("app.common.device_identity._runtime_machine_id", return_value="2c:cf:67:97:8c:8f"):
                first = Settings.from_env()

            self.assertEqual(first.device_id, "58:47:ca:7e:38:57")
            self.assertEqual(device_id_file.read_text(encoding="utf-8").strip(), "58:47:ca:7e:38:57")

            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True), mock.patch(
                "app.common.device_identity._physical_mac_candidates",
                return_value=["2c:cf:67:97:8c:8f"],
            ), mock.patch("app.common.device_identity._runtime_machine_id", return_value="aa:bb:cc:dd:ee:ff"):
                restarted = Settings.from_env()

            self.assertEqual(restarted.device_id, "58:47:ca:7e:38:57")

    def test_configured_device_id_wins_without_rewriting_persisted_id(self) -> None:
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

            self.assertEqual(settings.device_id, "bff-drone-a")
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

    def test_peer_address_candidates_default_to_tailnet_then_host_then_ip(self) -> None:
        from app.transfer.peer_connectivity import _peer_address_candidates

        peer = {
            "reachable_url": "https://192.168.0.180",
            "advertised_reachable_url": "https://BATOCERA-LAPTOP.local",
            "tailnet_ip": "100.91.173.37",
            "scheme": "https",
            "api_port": 443,
        }
        self.assertEqual(
            _peer_address_candidates(peer),
            [
                "https://100.91.173.37",
                "https://BATOCERA-LAPTOP.local",
                "https://192.168.0.180",
            ],
        )

    def test_peer_json_falls_back_from_tailnet_to_ip_with_short_timeout(self) -> None:
        from app.transfer import peer_connectivity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()
            peer = {
                "reachable_url": "https://192.168.0.180",
                "tailnet_ip": "100.91.173.37",
                "scheme": "https",
                "api_port": 443,
            }
            with mock.patch.object(
                peer_connectivity,
                "_peer_get_json",
                side_effect=[URLError("timed out"), {"systems": ["snes"]}],
            ) as get_json:
                payload, address = peer_connectivity._peer_get_json_for_peer(
                    peer,
                    "/v1/api/peer/inventory/summary",
                    settings,
                    peer_id="peer-id",
                    config={"network_mode": "local_network"},
                    timeout=120,
                )
            cached = load_peer_route(database_path(settings.userdata_root), "peer-id")

        self.assertEqual(payload, {"systems": ["snes"]})
        self.assertEqual(address, "https://192.168.0.180")
        self.assertEqual(get_json.call_args_list[0].args[0], "https://100.91.173.37/v1/api/peer/inventory/summary")
        self.assertEqual(get_json.call_args_list[0].kwargs["timeout"], 3)
        self.assertEqual(get_json.call_args_list[1].args[0], "https://192.168.0.180/v1/api/peer/inventory/summary")
        self.assertEqual(get_json.call_args_list[1].kwargs["timeout"], 120)
        self.assertEqual(cached["address"], "https://192.168.0.180")
        self.assertEqual(cached["route_kind"], "ip")

    def test_peer_json_overall_deadline_stops_trying_once_budget_is_spent(self) -> None:
        # Regression: without an overall deadline, a fully-offline peer with N
        # candidate addresses takes (N-1) * short_timeout + timeout, not
        # timeout, since the per-candidate cap only bounds each *individual*
        # attempt (this is exactly what made the Swarm/Transfers pages slow
        # whenever any paired peer was offline). With a deadline, once the
        # budget is spent, remaining candidates are skipped rather than tried
        # with a token/near-zero timeout.
        from app.transfer import peer_connectivity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()
            peer = {
                "tailnet_ip": "100.64.0.5",
                "advertised_reachable_url": "https://drone-b.local",
                "reachable_url": "https://192.168.1.50",
                "scheme": "https",
                "api_port": 443,
            }
            with mock.patch.object(
                peer_connectivity, "_peer_get_json", side_effect=URLError("timed out")
            ) as get_json, mock.patch.object(
                peer_connectivity.time, "monotonic", side_effect=[1.0, 8.0, 11.0]
            ):
                with self.assertRaises(URLError):
                    peer_connectivity._peer_get_json_for_peer(
                        peer,
                        "/v1/api/peer/inventory/summary",
                        settings,
                        peer_id="peer-id",
                        config={"network_mode": "local_network"},
                        timeout=4,
                        overall_deadline=10.0,
                    )
        # Third candidate never attempted: the deadline check before it saw
        # remaining <= 0 (11.0 > 10.0) and broke out instead of calling in.
        self.assertEqual(get_json.call_count, 2)
        self.assertEqual(get_json.call_args_list[0].args[0], "https://100.64.0.5/v1/api/peer/inventory/summary")
        self.assertEqual(get_json.call_args_list[1].args[0], "https://drone-b.local/v1/api/peer/inventory/summary")

    def test_peer_json_without_a_deadline_keeps_prior_behavior(self) -> None:
        # No overall_deadline passed -> byte-for-byte the old behavior (every
        # candidate tried, non-final ones capped at the short timeout). This
        # guards that the new parameter is purely additive/opt-in.
        from app.transfer import peer_connectivity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()
            peer = {
                "tailnet_ip": "100.64.0.5",
                "reachable_url": "https://192.168.1.50",
                "scheme": "https",
                "api_port": 443,
            }
            with mock.patch.object(
                peer_connectivity, "_peer_get_json", side_effect=[URLError("timed out"), {"ok": True}]
            ) as get_json:
                payload, address = peer_connectivity._peer_get_json_for_peer(
                    peer, "/v1/api/peer/inventory/summary", settings, peer_id="peer-id",
                    config={"network_mode": "local_network"}, timeout=120,
                )
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(address, "https://192.168.1.50")
        self.assertEqual(get_json.call_count, 2)
        self.assertEqual(get_json.call_args_list[0].kwargs["timeout"], 3)
        self.assertEqual(get_json.call_args_list[1].kwargs["timeout"], 120)

    def test_cached_peer_route_is_preferred_when_still_authorized(self) -> None:
        from app.transfer.peer_connectivity import _peer_address_candidates

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()
            save_peer_route(
                database_path(settings.userdata_root),
                "peer-id",
                "https://BATOCERA-LAPTOP.local",
                "host",
            )
            peer = {
                "drone_id": "peer-id",
                "reachable_url": "https://192.168.0.180",
                "advertised_reachable_url": "https://BATOCERA-LAPTOP.local",
                "tailnet_ip": "100.91.173.37",
            }
            self.assertEqual(
                _peer_address_candidates(peer, settings=settings),
                [
                    "https://BATOCERA-LAPTOP.local",
                    "https://100.91.173.37",
                    "https://192.168.0.180",
                ],
            )

    def test_peer_route_cache_upserts_single_indexed_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = database_path(Path(tmp) / "userdata")
            save_peer_route(path, "peer-id", "https://100.91.173.37", "tailnet")
            save_peer_route(path, "peer-id", "https://BATOCERA-LAPTOP.local", "host")

            self.assertEqual(load_peer_route(path, "peer-id")["address"], "https://BATOCERA-LAPTOP.local")
            with open_database(path) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM peer_route_cache WHERE peer_id = ?",
                    ("peer-id",),
                ).fetchone()[0]
                plan = connection.execute(
                    "EXPLAIN QUERY PLAN SELECT address FROM peer_route_cache WHERE peer_id = ?",
                    ("peer-id",),
                ).fetchall()
            self.assertEqual(count, 1)
            self.assertTrue(any("SEARCH peer_route_cache" in str(row[3]) for row in plan))

            with self.assertRaisesRegex(ValueError, "unsupported peer route kind"):
                save_peer_route(path, "peer-id", "https://example.test", "unknown")

    def test_peer_health_url_uses_public_health_endpoint(self) -> None:
        self.assertEqual(_peer_health_url("https://198.51.100.21"), "https://198.51.100.21/health")
        self.assertEqual(_peer_health_url("https://bff-drone-b:443/"), "https://bff-drone-b:443/health")

    def test_game_log_collection_detects_launch_with_fingerprint(self) -> None:
        from app.device.game_activity import collect_game_logs

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

            result = collect_game_logs(settings, RomRepository(roms_root, root / "bios"))
            self.assertEqual(len(result["sessions"]), 1)
            session = result["sessions"][0]
            self.assertEqual(session["system_name"], "snes")
            self.assertEqual(session["game_name"], "Game.sfc")
            self.assertEqual(session["rom_path"], rom.resolve().as_posix())
            self.assertEqual(session["rom_fingerprint"], RomRepository.build_fingerprint(rom))
            self.assertEqual(session["played_at"], "2026-05-26T10:15:00+00:00")

    def test_game_log_collection_detects_batocera_v43_launch(self) -> None:
        from app.device.game_activity import collect_game_logs

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

            result = collect_game_logs(settings, RomRepository(roms_root, root / "bios"))
            self.assertEqual(len(result["sessions"]), 1)
            session = result["sessions"][0]
            self.assertEqual(session["system_name"], "steam")
            self.assertEqual(session["game_name"], "243780_PixelJunk_Monsters_Ultimate.sh")
            self.assertEqual(session["rom_path"], rom.resolve().as_posix())
            self.assertEqual(session["played_at"], "2026-06-08T23:25:36+00:00")

    def test_game_event_spool_produces_session_with_duration(self) -> None:
        from app.device.game_activity import collect_game_event_sessions, delete_game_event_spool, load_gameplay_history

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
            self.assertEqual(load_gameplay_history(settings), [session])

    def test_game_process_monitor_emits_start_and_stop_events(self) -> None:
        from app.device.game_activity import GameProcessMonitor, collect_game_event_sessions, find_running_emulatorlauncher

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            roms_root = root / "roms"
            rom = roms_root / "snes" / "Game With Spaces.sfc"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"rom-data")
            proc_root = Path(tmp) / "proc"
            process_dir = proc_root / "123"
            process_dir.mkdir(parents=True)
            (process_dir / "cmdline").write_bytes(
                f"/usr/bin/python3\x00/usr/bin/emulatorlauncher\x00-system\x00snes\x00-rom\x00{rom}\x00".encode()
            )
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(roms_root)},
                clear=True,
            ):
                settings = Settings.from_env()

            running = find_running_emulatorlauncher(proc_root)
            self.assertEqual(running["system_name"], "snes")
            self.assertEqual(running["rom_path"], str(rom))

            monitor = GameProcessMonitor(settings, proc_root=proc_root)
            monitor.poll_once()
            sessions, start_events = collect_game_event_sessions(settings, RomRepository(roms_root, root / "bios"))
            self.assertEqual(sessions, [])
            self.assertEqual(len(start_events), 1)
            for event in start_events:
                event.unlink()

            (process_dir / "cmdline").unlink()
            monitor.poll_once()

            sessions, processed = collect_game_event_sessions(settings, RomRepository(roms_root, root / "bios"))
            self.assertEqual(len(processed), 1)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["system_name"], "snes")
            self.assertEqual(sessions[0]["rom_path"], rom.resolve().as_posix())
            self.assertIn("duration_seconds", sessions[0])

    def test_game_process_monitor_retries_failed_spool_write(self) -> None:
        from app.device.game_activity import GameProcessMonitor

        monitor = GameProcessMonitor(mock.Mock())
        running = {"system_name": "snes", "rom_path": "/userdata/roms/snes/Game.sfc"}
        with mock.patch("app.device.game_activity.find_running_emulatorlauncher", return_value=running):
            with mock.patch("app.device.game_activity.write_game_process_event", side_effect=[None, Path("/tmp/start.json")]) as write_event:
                monitor.poll_once()
                self.assertIsNone(monitor.active_game)
                monitor.poll_once()

        self.assertEqual(write_event.call_count, 2)
        self.assertEqual(monitor.active_game["rom_path"], running["rom_path"])

    def test_emulator_config_listing_uses_allowed_batocera_paths(self) -> None:
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

            result = list_emulator_config_files(settings)
            rows = {row["relative_path"] for row in result["configs"]}

            self.assertEqual(rows, {
                "dolphin-emu/Dolphin.ini",
                "emulationstation/es_settings.cfg",
                "rpcs3/patches/patch.yml",
                "shadps4/user/patches/enabled.yml",
                "pcmanfm/default/pcmanfm.conf",
            })
            self.assertFalse(any(path.name in str(rows) for path in excluded))

    def test_emulator_config_listing_can_return_full_snapshot_for_local_ui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            configs = root / "system" / "configs" / "retroarch"
            configs.mkdir(parents=True)
            for index in range(251):
                (configs / f"{index:03}.cfg").write_text(str(index), encoding="utf-8")
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()

            snapshot = list_emulator_config_files(settings, max_configs=0)

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

    def test_emulator_config_list_default_limit(self) -> None:
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

    def test_emulator_config_list_applies_filter_and_page_before_stat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            configs = root / "system" / "configs" / "retroarch"
            configs.mkdir(parents=True)
            for name in ("alpha.cfg", "beta.cfg", "gamma.cfg"):
                (configs / name).write_text(name, encoding="utf-8")
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()

            listing = list_emulator_config_files(settings, max_configs=1, offset=1, query=".cfg")

            self.assertEqual(listing["total"], 3)
            self.assertEqual(listing["offset"], 1)
            self.assertEqual(
                [row["relative_path"] for row in listing["configs"]],
                ["retroarch/beta.cfg"],
            )

    def test_peer_trust_cafile_uses_local_pinned_certificate_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "local-a"}, clear=True):
                settings = Settings.from_env()
            local_cert = drone_api._local_peer_cert_cache_path(settings, "local-b")
            local_cert.parent.mkdir(parents=True)
            local_cert.write_text("local-peer-cert", encoding="utf-8")

            self.assertEqual(_peer_trust_cafile(settings, peer_id="local-b", config={}), local_cert)
            # No pinned cert cached yet for this peer -> nothing to trust.
            self.assertIsNone(_peer_trust_cafile(settings, peer_id="unknown-peer", config={}))
            # No peer id at all -> nothing to trust.
            self.assertIsNone(_peer_trust_cafile(settings, peer_id=None, config={}))

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

    def test_download_rom_from_peer_uses_pinned_peer_cert_and_reachable_url(self) -> None:
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
            pinned_cert = drone_api._local_peer_cert_cache_path(settings, "bff-drone-b")
            pinned_cert.parent.mkdir(parents=True)
            pinned_cert.write_text("peer-cert", encoding="utf-8")

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
            with mock.patch(
                "app.transfer.peer_download._drone_client_ssl_context", side_effect=fake_context
            ), mock.patch(
                "app.transfer.peer_download.urlopen", side_effect=fake_urlopen
            ):
                result = _download_rom_from_peer(settings, {}, peer, "atari7800", "Asteroids (USA).zip", expected_size=7)

            self.assertEqual(result["source_drone_id"], "bff-drone-b")
            self.assertEqual(requests[0][0], "https://bff-drone-b:443/v1/api/peer/roms/atari7800/Asteroids%20%28USA%29.zip")
            self.assertEqual(contexts[0][1], True)
            self.assertEqual(contexts[0][2], pinned_cert)
            self.assertEqual((root / "roms" / "atari7800" / "Asteroids (USA).zip").read_bytes(), b"ROMDATA")

    def test_download_rom_from_peer_skips_existing_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            target = root / "roms" / "nes" / "Game.nes"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"already here")

            with mock.patch("app.transfer.peer_download.urlopen", side_effect=AssertionError("duplicate download attempted")):
                result = _download_rom_from_peer(
                    settings,
                    {},
                    {"drone_id": "source-a", "reachable_url": "http://source-a:8080"},
                    "nes",
                    "Game.nes",
                )

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["skip_reason"], "target path already exists")
            self.assertFalse((root / "roms" / "nes" / "Game (2).nes").exists())

    def test_download_rom_from_peer_overwrites_existing_target_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            target = root / "roms" / "nes" / "Game.nes"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")

            class FakeResponse:
                headers = {}

                def __init__(self):
                    self.chunks = [b"replacement", b""]

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self, _size=-1):
                    return self.chunks.pop(0)

            with mock.patch("app.transfer.peer_download.urlopen", return_value=FakeResponse()):
                result = _download_rom_from_peer(
                    settings, {}, {"drone_id": "source-a", "reachable_url": "http://source-a:8080"},
                    "nes", "Game.nes", overwrite=True,
                )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(target.read_bytes(), b"replacement")

    def test_download_rom_from_peer_skips_matching_local_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            existing = root / "roms" / "nes" / "Existing Name.nes"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"same rom bytes")
            fingerprint = RomRepository.build_fingerprint(existing)

            with mock.patch("app.transfer.peer_download.urlopen", side_effect=AssertionError("duplicate download attempted")):
                result = _download_rom_from_peer(
                    settings,
                    {},
                    {"drone_id": "source-a", "reachable_url": "http://source-a:8080"},
                    "nes",
                    "Peer Name.nes",
                    expected_fingerprint=fingerprint,
                )

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["skip_reason"], "matching ROM already exists")
            self.assertEqual(result["relative_path"], "Existing Name.nes")
            self.assertFalse((root / "roms" / "nes" / "Peer Name.nes").exists())

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
            with mock.patch("app.transfer.peer_download.urlopen", return_value=FakeResponse()):
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
            with mock.patch("app.transfer.peer_download._peer_get_json", return_value=manifest), mock.patch(
                "app.transfer.peer_download.urlopen", side_effect=fake_urlopen
            ), mock.patch.object(RomRepository, "build_fingerprint", side_effect=AssertionError("folder sync should not hash")):
                result = _download_rom_folder_from_peer(settings, {}, peer, "ps3", "Game.ps3", expected_size=10)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["entry_type"], "folder")
            self.assertEqual(result["bytes_transferred"], 10)
            self.assertEqual((root / "roms" / "ps3" / "Game.ps3" / "PS3_GAME" / "PARAM.SFO").read_bytes(), b"param")
            self.assertEqual((root / "roms" / "ps3" / "Game.ps3" / "PS3_GAME" / "USRDIR" / "EBOOT.BIN").read_bytes(), b"eboot")

    def test_download_folder_unit_rom_marker_last_skip_and_verify(self) -> None:
        """Folder-unit ROMs (marker file in a per-game folder): the marker is written
        LAST, its fingerprint is verified, and a present marker skips the re-download."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()

            marker_bytes = b"gdi index"
            track_bytes = b"track bytes"
            fingerprint_probe = Path(tmp) / "probe.gdi"
            fingerprint_probe.write_bytes(marker_bytes)
            expected_fingerprint = RomRepository.build_fingerprint(fingerprint_probe)

            # Manifest lists the marker FIRST; the receiver must reorder it last.
            manifest = {
                "relative_path": "Game",
                "entry_type": "folder",
                "file_size": len(marker_bytes) + len(track_bytes),
                "directories": [],
                "files": [
                    {"relative_path": "Game.gdi", "file_size": len(marker_bytes)},
                    {"relative_path": "track01.bin", "file_size": len(track_bytes)},
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

            fetched = []

            def fake_urlopen(request, timeout=None, context=None):
                url = request.full_url
                if url.endswith("/Game/Game.gdi"):
                    fetched.append("Game.gdi")
                    return FakeResponse(marker_bytes)
                if url.endswith("/Game/track01.bin"):
                    fetched.append("track01.bin")
                    return FakeResponse(track_bytes)
                raise AssertionError(url)

            peer = {"drone_id": "source-a", "reachable_url": "http://source-a:8080"}
            kwargs = dict(
                expected_size=len(marker_bytes) + len(track_bytes),
                expected_fingerprint=expected_fingerprint,
                marker_relative_path="Game/Game.gdi",
            )
            with mock.patch("app.transfer.peer_download._peer_get_json", return_value=manifest), mock.patch(
                "app.transfer.peer_download.urlopen", side_effect=fake_urlopen
            ):
                result = _download_rom_folder_from_peer(settings, {}, peer, "dreamcast", "Game", **kwargs)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(fetched, ["track01.bin", "Game.gdi"], "marker must be written last")
            self.assertEqual(result["marker_relative_path"], "Game/Game.gdi")
            self.assertEqual(result["rom_fingerprint"], expected_fingerprint)
            game_dir = root / "roms" / "dreamcast" / "Game"
            self.assertEqual((game_dir / "Game.gdi").read_bytes(), marker_bytes)
            self.assertEqual((game_dir / "track01.bin").read_bytes(), track_bytes)

            # Present marker (matching fingerprint) -> skipped without touching the peer.
            with mock.patch(
                "app.transfer.peer_download._peer_get_json", side_effect=AssertionError("must not fetch manifest")
            ), mock.patch("app.transfer.peer_download.urlopen", side_effect=AssertionError("must not download")):
                skipped = _download_rom_folder_from_peer(settings, {}, peer, "dreamcast", "Game", **kwargs)
            self.assertEqual(skipped["status"], "skipped")
            self.assertEqual(skipped["skip_reason"], "folder ROM marker already exists")

            # Wrong expected fingerprint -> the transfer fails and removes the marker
            # so the present-check cannot accept the bad folder later.
            bad_kwargs = dict(kwargs, expected_fingerprint="0" * 32)
            (game_dir / "Game.gdi").unlink()  # fresh marker so the present-check passes through
            with mock.patch("app.transfer.peer_download._peer_get_json", return_value=manifest), mock.patch(
                "app.transfer.peer_download.urlopen", side_effect=fake_urlopen
            ):
                with self.assertRaises(RuntimeError):
                    _download_rom_folder_from_peer(settings, {}, peer, "dreamcast", "Game", **bad_kwargs)
            self.assertFalse((game_dir / "Game.gdi").exists())

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

            queued = manager.enqueue_rom({}, {"drone_id": "source-a"}, "fbneo", "1943.zip", expected_size=123)
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

    def test_download_manager_restores_interrupted_jobs_after_restart(self) -> None:
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
            with mock.patch("app.transfer.download_manager.Thread.start"):
                first_manager = DownloadManager(settings, repo)
                queued = first_manager.enqueue_rom(
                    {},
                    {"drone_id": "source-a", "reachable_url": "https://source-a.local"},
                    "snes",
                    "Game.zip",
                    expected_size=100,
                )
                with first_manager._lock:
                    interrupted = first_manager._jobs[queued["job_id"]]
                    interrupted["status"] = "downloading"
                    interrupted["downloaded_bytes"] = 60
                    interrupted["bytes_transferred"] = 60
                    first_manager._persist_state_locked()
                restored_manager = DownloadManager(settings, repo)

            restored = restored_manager.snapshot()["queued"]
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0]["job_id"], queued["job_id"])
            self.assertEqual(restored[0]["downloaded_bytes"], 0)
            self.assertIn("restarted", restored[0]["resume_reason"])

    def test_download_manager_preserves_manual_pause_across_restart(self) -> None:
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
            with mock.patch("app.transfer.download_manager.Thread.start"):
                first_manager = DownloadManager(settings, repo)
                first_manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "Game.zip")
                first_manager.pause()
                restored_manager = DownloadManager(settings, repo)
            self.assertTrue(restored_manager.snapshot()["paused"])
            self.assertEqual(len(restored_manager.snapshot()["queued"]), 1)

    def _make_download_manager(self, root: Path, extra_env: dict = None) -> "DownloadManager":
        with mock.patch.dict(
            "os.environ",
            {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "OVERMIND_DEVICE_ID": "target-a",
                **(extra_env or {}),
            },
            clear=True,
        ):
            settings = Settings.from_env()
        repo = RomRepository(root / "roms", root / "bios")
        with mock.patch("app.transfer.download_manager.Thread.start"):
            return DownloadManager(settings, repo)

    def test_download_manager_pause_and_resume_queued_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._make_download_manager(Path(tmp) / "userdata")
            queued = manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "Game.zip")
            result = manager.pause_job(queued["job_id"])
            self.assertEqual(result["status"], "paused")
            snapshot = manager.snapshot()
            self.assertEqual(len(snapshot["queued"]), 1)
            self.assertEqual(snapshot["queued"][0]["status"], "paused")
            self.assertIsNone(snapshot["queued"][0]["queue_position"])

            resumed = manager.resume_job(queued["job_id"])
            self.assertEqual(resumed["status"], "queued")
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["queued"][0]["status"], "queued")
            self.assertEqual(snapshot["queued"][0]["queue_position"], 1)

    def test_download_manager_pause_downloading_job_lands_paused_on_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._make_download_manager(Path(tmp) / "userdata")
            queued = manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "Game.zip")
            job_id = queued["job_id"]
            with manager._lock:
                job = manager._jobs[job_id]
                job["status"] = "downloading"
                job["downloaded_bytes"] = 42
                job["bytes_transferred"] = 42

            pause_result = manager.pause_job(job_id)
            self.assertEqual(pause_result["status"], "pausing")
            with manager._lock:
                self.assertTrue(manager._jobs[job_id]["pause_requested"])
                self.assertTrue(manager._cancel_events[job_id].is_set())

            # Simulate the in-flight fetch observing the cancellation event and
            # raising, as the real transports do -- _run_job must land it
            # 'paused' (not 'cancelled'/'failed') because pause_requested is set.
            with mock.patch.object(manager._selector, "fetch", side_effect=DownloadCancelled("stopped for pause")):
                manager._run_job(job_id)

            snapshot = manager.snapshot()
            self.assertEqual(len(snapshot["queued"]), 1)
            self.assertEqual(snapshot["queued"][0]["status"], "paused")
            self.assertEqual(snapshot["queued"][0]["downloaded_bytes"], 0)

            resumed = manager.resume_job(job_id)
            self.assertEqual(resumed["status"], "queued")

    def test_download_manager_pause_not_allowed_on_terminal_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._make_download_manager(Path(tmp) / "userdata")
            queued = manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "Game.zip")
            manager.cancel(queued["job_id"])
            result = manager.pause_job(queued["job_id"])
            self.assertEqual(result["status"], "not_pausable")

    def test_download_manager_restart_preserves_per_job_paused_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            first_manager = self._make_download_manager(root)
            queued = first_manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "Paused.zip")
            first_manager.pause_job(queued["job_id"])

            with mock.patch("app.transfer.download_manager.Thread.start"):
                restored_manager = DownloadManager(first_manager.settings, first_manager.repository)
            snapshot = restored_manager.snapshot()
            statuses = {job["job_id"]: job["status"] for job in snapshot["queued"]}
            self.assertEqual(statuses[queued["job_id"]], "paused")

    def test_download_manager_requeues_transient_transport_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "OVERMIND_DEVICE_ID": "target-a",
                    "DOWNLOAD_RECONNECT_INITIAL_SECONDS": "1",
                    "DOWNLOAD_RECONNECT_MAX_SECONDS": "1",
                },
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(root / "roms", root / "bios")
            with mock.patch("app.transfer.download_manager.Thread.start"):
                manager = DownloadManager(settings, repo)
            queued = manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "Game.zip")
            with mock.patch.object(manager._selector, "fetch", side_effect=OSError("connection refused")):
                manager._run_job(queued["job_id"])
            restored = manager.snapshot()["queued"][0]
            self.assertEqual(restored["status"], "queued")
            self.assertEqual(restored["reconnect_attempts"], 1)
            self.assertGreater(restored["reconnect_after_epoch"], time.time())
            self.assertIn("reconnecting", restored["resume_reason"])

    def test_download_manager_refreshes_paired_peer_route_before_running_job(self) -> None:
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
                with mock.patch("app.transfer.download_manager.Thread.start"):
                    manager = DownloadManager(settings, repo)
                queued = manager.enqueue_rom(
                    {}, {"drone_id": "source-a", "reachable_url": "https://old-address"}, "snes", "Game.zip",
                )
                # A route learned since the job was queued (e.g. a successful
                # pairing re-check) must be picked up, not a stale snapshot.
                local_network.save_paired_peer(
                    settings, {"drone_id": "source-a", "reachable_url": "https://new-address"},
                )
                with manager._lock:
                    job = manager._jobs[queued["job_id"]]
                    manager._refresh_connection_metadata_locked(job)
                    refreshed_peer = dict(job["_peer"])
            self.assertEqual(refreshed_peer["reachable_url"], "https://new-address")

    def test_download_manager_keeps_permanent_validation_failure_terminal(self) -> None:
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
            with mock.patch("app.transfer.download_manager.Thread.start"):
                manager = DownloadManager(settings, repo)
            queued = manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "../unsafe.zip")
            with mock.patch.object(manager._selector, "fetch", side_effect=ValueError("invalid target path")):
                manager._run_job(queued["job_id"])
            self.assertEqual(manager.snapshot()["recent"][0]["status"], "failed")

    def test_download_manager_estimates_entire_queue_completion(self) -> None:
        estimate = DownloadManager._queue_estimate(
            active=[{"total_bytes": 1000, "downloaded_bytes": 400, "transfer_speed_bps": 100}],
            queued=[{"total_bytes": 2000}, {"total_bytes": None}],
            recent=[],
            paused=False,
        )

        self.assertEqual(estimate["queue_known_remaining_bytes"], 2600)
        self.assertEqual(estimate["queue_estimated_unknown_bytes"], 1500)
        self.assertEqual(estimate["queue_remaining_bytes"], 4100)
        self.assertEqual(estimate["queue_eta_seconds"], 41)
        self.assertEqual(estimate["queue_unknown_size_count"], 1)
        self.assertEqual(estimate["queue_estimate_speed_source"], "active")
        self.assertEqual(estimate["queue_eta_state"], "ready")

        paused = DownloadManager._queue_estimate([], [{"total_bytes": 500}], [{"status": "completed", "transfer_speed_bps": 50}], True)
        self.assertEqual(paused["queue_eta_seconds"], 10)
        self.assertEqual(paused["queue_estimate_speed_source"], "recent")
        self.assertEqual(paused["queue_eta_state"], "paused")

        calculating = DownloadManager._queue_estimate([], [{"total_bytes": None}], [], False)
        self.assertIsNone(calculating["queue_remaining_bytes"])
        self.assertFalse(calculating["queue_size_estimate_available"])
        self.assertIsNone(calculating["queue_eta_seconds"])
        self.assertEqual(calculating["queue_eta_state"], "calculating")

    def test_download_manager_concurrency_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios"), "OVERMIND_DEVICE_ID": "target-a"},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(root / "roms", root / "bios")

            with mock.patch.dict("os.environ", {"DRONE_DOWNLOAD_CONCURRENCY": "4"}, clear=False), \
                 mock.patch("app.drone_api.Thread.start"):
                manager = DownloadManager(settings, repo)
            self.assertEqual(manager._concurrency, 4)
            self.assertEqual(len(manager._threads), 4)
            self.assertEqual(manager.snapshot()["concurrency"]["active_limit"], 4)

            # Clamp out-of-range / non-numeric values; default to 3 when unset.
            for raw, expected in (("0", 1), ("99", 8), ("abc", 3)):
                with mock.patch.dict("os.environ", {"DRONE_DOWNLOAD_CONCURRENCY": raw}, clear=False), \
                     mock.patch("app.drone_api.Thread.start"):
                    self.assertEqual(DownloadManager(settings, repo)._concurrency, expected, raw)
            with mock.patch("app.drone_api.Thread.start"):
                os.environ.pop("DRONE_DOWNLOAD_CONCURRENCY", None)
                self.assertEqual(DownloadManager(settings, repo)._concurrency, 3)

    def test_queue_estimate_uses_aggregate_throughput(self) -> None:
        # Three streams at 4 MB/s each drain 90 MB at 12 MB/s (~7s), not 22s.
        active = [{"total_bytes": 30_000_000, "downloaded_bytes": 0, "transfer_speed_bps": 4_000_000} for _ in range(3)]
        est = DownloadManager._queue_estimate(active, [], [], False, concurrency=3)
        self.assertEqual(est["queue_estimate_speed_bps"], 12_000_000)
        self.assertEqual(est["queue_eta_seconds"], 7)

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

    def test_bios_inventory_fingerprint_is_stable_and_order_independent(self) -> None:
        left = drone_api._bios_inventory_fingerprint([
            {"relative_path": "./SNES\\bios.bin", "bios_md5": "ABC", "file_size": 12},
            {"file_path": "nes/disksys.rom", "md5": "def", "byte_count": 4},
        ])
        right = drone_api._bios_inventory_fingerprint([
            {"path": "nes/disksys.rom", "bios_md5": "DEF", "file_size": 4},
            {"relative_path": "snes/bios.bin", "fingerprint": "abc", "file_size": 12},
        ])
        self.assertEqual(left, right)
        # A different BIOS set produces a different thumbprint.
        self.assertNotEqual(left, drone_api._bios_inventory_fingerprint([
            {"relative_path": "snes/bios.bin", "bios_md5": "abc", "file_size": 99},
        ]))

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

    def test_gamelist_rom_metadata_ignores_disk_rows_missing_from_gamelist(self) -> None:
        # Strict gamelist-as-source-of-truth: a ROM present on disk but absent from
        # gamelist.xml is NOT reported, so the gamelist.xml MD5 stays a complete change
        # signal (a ROM outside the gamelist can't change what we report/upload).
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
            self.assertEqual(set(by_path), {"Known.zip"})
            self.assertTrue(by_path["Known.zip"]["has_gamelist_entry"])
            self.assertEqual(by_path["Known.zip"]["metadata_source"], "gamelist.xml")

    def test_gamelist_rom_metadata_empty_when_gamelist_absent(self) -> None:
        # Strict mode: no gamelist.xml -> zero games reported, even if ROMs sit on disk.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rom = root / "roms" / "fbneo" / "1942.zip"
            rom.parent.mkdir(parents=True)
            rom.write_bytes(b"rom")
            repo = RomRepository(root / "roms", root / "bios")

            gamelist, roms = repo.list_gamelist_rom_metadata("fbneo")

            self.assertEqual(roms, [])
            self.assertFalse(gamelist["exists"])

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

    def test_privileged_screen_mode_helper_updates_xml_and_restarts_emulationstation(self) -> None:
        from app import set_screen_mode

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "es_settings.cfg"
            config.write_text('<?xml version="1.0"?><map><string name="ThemeSet" value="carbon"/></map>', encoding="utf-8")
            with mock.patch.object(set_screen_mode, "CONFIG", config):
                with mock.patch("app.set_screen_mode.subprocess.run", return_value=mock.Mock(returncode=0, stdout="")) as run:
                    set_screen_mode.set_screen_mode("kiosk")
                    self.assertIn('name="UIMode" value="Kiosk"', config.read_text(encoding="utf-8"))
                    set_screen_mode.set_screen_mode("full")
                    set_screen_mode.set_screen_mode("kid")

            self.assertIn('name="ThemeSet" value="carbon"', config.read_text(encoding="utf-8"))
            self.assertIn('name="UIMode" value="Kid"', config.read_text(encoding="utf-8"))
            # Each invocation synchronously runs stop ES + save overlay + start ES so
            # success is not reported before the display service accepted the start.
            run_commands = [call.args[0] for call in run.call_args_list]
            lifecycle_commands = [command for command in run_commands if command != ["pidof", "emulationstation"]]
            self.assertEqual(len(lifecycle_commands), 9)
            self.assertEqual(run_commands.count(["pidof", "emulationstation"]), 3)
            self.assertIn([set_screen_mode.EMULATIONSTATION_SERVICE, "stop"], run_commands)
            self.assertIn(["batocera-save-overlay"], run_commands)
            self.assertIn([set_screen_mode.EMULATIONSTATION_SERVICE, "start"], run_commands)

    def test_screen_mode_retries_when_start_returns_without_emulationstation(self) -> None:
        from app import set_screen_mode

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "es_settings.cfg"
            with mock.patch("app.set_screen_mode.subprocess.run", return_value=mock.Mock(returncode=0, stdout="")) as run, \
                 mock.patch("app.set_screen_mode._wait_for_emulationstation", side_effect=[False, True]) as wait, \
                 mock.patch("app.set_screen_mode.time.sleep"):
                set_screen_mode.set_screen_mode("kiosk", config=config)

            start_command = [set_screen_mode.EMULATIONSTATION_SERVICE, "start"]
            self.assertEqual([call.args[0] for call in run.call_args_list].count(start_command), 2)
            self.assertEqual(wait.call_count, 2)

    def test_privileged_screen_mode_helper_skips_restart_when_mode_is_unchanged(self) -> None:
        from app import set_screen_mode

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "es_settings.cfg"
            config.write_text(
                '<?xml version="1.0"?><map><string name="UIMode" value="Full"/></map>',
                encoding="utf-8",
            )
            with mock.patch("app.set_screen_mode.subprocess.run") as run:
                restarted = set_screen_mode.set_screen_mode("full", config=config)

            self.assertFalse(restarted)
            run.assert_not_called()

    def test_non_root_screen_mode_skips_worker_when_mode_is_unchanged(self) -> None:
        from app.device import device_control

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            es_settings = root / "system" / "configs" / "emulationstation" / "es_settings.cfg"
            es_settings.parent.mkdir(parents=True)
            es_settings.write_text(
                '<?xml version="1.0"?><map><string name="UIMode" value="Full"/></map>',
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ES_SETTINGS_FILE": str(es_settings)},
                clear=True,
            ):
                settings = Settings.from_env()
            with mock.patch("app.device.device_control.os.geteuid", return_value=999), \
                 mock.patch("app.device.device_control._request_screen_mode_service_control") as dispatch:
                path, restarted = device_control._apply_screen_mode(settings, "full")

            self.assertEqual(path, es_settings)
            self.assertFalse(restarted)
            dispatch.assert_not_called()

    def test_screen_mode_start_does_not_capture_output_via_pipe(self) -> None:
        # Regression test: "start" backgrounds `startx &` without redirecting its
        # own stdout/stderr, so a backgrounded EmulationStation/X process tree
        # keeps the *inherited* pipe write-end open indefinitely. Capturing via
        # stdout=PIPE (as every other step does) would make subprocess.run block
        # until that pipe sees EOF -- which never happens while ES keeps running
        # -- so every "start" would spuriously time out, even a fully successful
        # one, and trigger an unnecessary/conflicting swissknife-restart fallback.
        # Confirmed live: this was wedging the device into a permanent crash loop.
        from app import set_screen_mode

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "es_settings.cfg"
            calls = []

            def fake_run(command, **kwargs):
                calls.append((command, kwargs))
                return mock.Mock(returncode=0, stdout="")

            with mock.patch.object(set_screen_mode, "CONFIG", config):
                with mock.patch("app.set_screen_mode.subprocess.run", side_effect=fake_run):
                    set_screen_mode.set_screen_mode("kiosk")

            start_calls = [kwargs for command, kwargs in calls if command == [set_screen_mode.EMULATIONSTATION_SERVICE, "start"]]
            self.assertEqual(len(start_calls), 1)
            self.assertEqual(start_calls[0].get("stdout"), subprocess.DEVNULL)
            self.assertEqual(start_calls[0].get("stderr"), subprocess.DEVNULL)

            stop_calls = [kwargs for command, kwargs in calls if command == [set_screen_mode.EMULATIONSTATION_SERVICE, "stop"]]
            self.assertEqual(stop_calls[0].get("stdout"), subprocess.PIPE)

    def test_screen_mode_helper_restarts_emulationstation_even_when_overlay_fails(self) -> None:
        from app import set_screen_mode

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "es_settings.cfg"

            def fake_run(command, **kwargs):
                # Simulate batocera-save-overlay failing/hanging in the headless context.
                rc = 1 if command and command[0] == "batocera-save-overlay" else 0
                return mock.Mock(returncode=rc, stdout="overlay failure" if rc else "")

            with mock.patch.object(set_screen_mode, "CONFIG", config):
                with mock.patch("app.set_screen_mode.subprocess.run", side_effect=fake_run):
                    set_screen_mode.set_screen_mode("kiosk")

            # The overlay step failed, but EmulationStation must still be restarted and the
            # new UIMode must still be written (so the screen comes back in Kiosk mode).
            self.assertIn('name="UIMode" value="Kiosk"', config.read_text(encoding="utf-8"))

    def test_screen_mode_helper_falls_back_when_service_start_fails(self) -> None:
        from app import set_screen_mode

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "es_settings.cfg"

            def fake_run(command, **kwargs):
                rc = 1 if command == [set_screen_mode.EMULATIONSTATION_SERVICE, "start"] else 0
                return mock.Mock(returncode=rc, stdout="start failure" if rc else "")

            with mock.patch.object(set_screen_mode, "CONFIG", config), \
                 mock.patch("app.set_screen_mode.subprocess.run", side_effect=fake_run) as run, \
                 mock.patch("app.set_screen_mode.shutil.which", return_value="/usr/bin/batocera-es-swissknife"):
                set_screen_mode.set_screen_mode("kiosk")

            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(["/usr/bin/batocera-es-swissknife", "--restart"], commands)

    def test_apply_audio_volume_clamps_and_dispatches_to_privileged_worker(self) -> None:
        from app.device import device_control

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                settings = Settings.from_env()
            with mock.patch.object(device_control.os, "geteuid", return_value=999), \
                 mock.patch.object(device_control, "_request_volume_service_control", return_value=True) as volume_control:
                self.assertEqual(device_control._apply_audio_volume(settings, 150), 100)
                self.assertEqual(device_control._apply_audio_volume(settings, -10), 0)
                self.assertEqual(device_control._apply_audio_volume(settings, 60), 60)
            volume_control.assert_has_calls([mock.call(100), mock.call(0), mock.call(60)])

    def test_privileged_volume_helper_uses_batocera_audio(self) -> None:
        from app import set_volume

        def fake_which(name):
            return f"/usr/bin/{name}" if name == "batocera-audio" else None

        with mock.patch("app.set_volume.shutil.which", side_effect=fake_which):
            with mock.patch("app.set_volume.subprocess.run", return_value=mock.Mock(returncode=0, stdout="")) as run:
                set_volume.set_audio_volume(40)
                set_volume.set_audio_volume(0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["/usr/bin/batocera-audio", "setSystemVolume", "40"], commands)
        self.assertIn(["/usr/bin/batocera-audio", "setSystemVolume", "0"], commands)

    def test_privileged_volume_helper_falls_back_to_amixer(self) -> None:
        from app import set_volume

        def fake_which(name):
            return f"/usr/bin/{name}" if name == "amixer" else None

        with mock.patch("app.set_volume.shutil.which", side_effect=fake_which):
            with mock.patch("app.set_volume.subprocess.run", return_value=mock.Mock(returncode=0, stdout="")) as run:
                set_volume.set_audio_volume(40)
                set_volume.set_audio_volume(0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["/usr/bin/amixer", "-q", "sset", "Master", "40%", "unmute"], commands)
        self.assertIn(["/usr/bin/amixer", "-q", "sset", "Master", "mute"], commands)

    def test_privileged_volume_helper_raises_with_command_output_on_failure(self) -> None:
        from app import set_volume

        def fake_which(name):
            return f"/usr/bin/{name}" if name == "batocera-audio" else None

        with mock.patch("app.set_volume.shutil.which", side_effect=fake_which):
            with mock.patch(
                "app.set_volume.subprocess.run",
                return_value=mock.Mock(returncode=1, stdout="no default sink"),
            ):
                with self.assertRaises(OSError) as ctx:
                    set_volume.set_audio_volume(40)
        self.assertIn("no default sink", str(ctx.exception))

    def test_privileged_volume_helper_requires_a_tool(self) -> None:
        from app import set_volume

        with mock.patch("app.set_volume.shutil.which", return_value=None):
            with self.assertRaises(OSError):
                set_volume.set_audio_volume(50)

    def test_emulationstation_restart_waits_for_worker_success(self) -> None:
        from app.device import device_control

        with tempfile.TemporaryDirectory() as tmp:
            control_dir = Path(tmp) / "control"

            def dispatch(_command: str, body=None) -> bool:
                control_dir.mkdir(parents=True, exist_ok=True)
                (control_dir / "restart-emulationstation.result").write_text("ok\n", encoding="utf-8")
                return True

            with mock.patch.dict("os.environ", {"DRONE_SERVICE_CONTROL_DIR": str(control_dir)}, clear=True), \
                 mock.patch("app.device.device_control._request_service_control", side_effect=dispatch) as request:
                self.assertTrue(device_control._request_emulationstation_restart_service_control())

            request.assert_called_once_with("restart-emulationstation")

    def test_emulationstation_restart_surfaces_worker_failure(self) -> None:
        from app.device import device_control

        with tempfile.TemporaryDirectory() as tmp:
            control_dir = Path(tmp) / "control"

            def dispatch(_command: str, body=None) -> bool:
                control_dir.mkdir(parents=True, exist_ok=True)
                (control_dir / "restart-emulationstation.result").write_text("ES did not return\n", encoding="utf-8")
                return True

            with mock.patch.dict("os.environ", {"DRONE_SERVICE_CONTROL_DIR": str(control_dir)}, clear=True), \
                 mock.patch("app.device.device_control._request_service_control", side_effect=dispatch):
                with self.assertRaisesRegex(OSError, "ES did not return"):
                    device_control._request_emulationstation_restart_service_control()

    def test_direct_emulationstation_restart_uses_delayed_stop_and_start(self) -> None:
        from app.device import device_control

        init_script = "/etc/init.d/S31emulationstation"
        with mock.patch(
            "app.device.device_control._request_emulationstation_restart_service_control",
            return_value=False,
        ), mock.patch(
            "app.device.device_control._emulationstation_restart_command",
            return_value=[init_script, "restart"],
        ), mock.patch(
            "app.device.device_control.subprocess.run",
            return_value=mock.Mock(returncode=0),
        ) as run, mock.patch(
            "app.device.device_control._wait_for_emulationstation_process",
            return_value=True,
        ), mock.patch("app.device.device_control.time.sleep") as sleep:
            self.assertTrue(device_control._restart_emulationstation())

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands, [[init_script, "stop"], [init_script, "start"]])
        self.assertNotIn([init_script, "restart"], commands)
        sleep.assert_called_once_with(3)

    def test_purge_asset_cache_keep_fingerprint_clears_entries_but_preserves_fingerprint(self) -> None:
        from app.storage.rom_metadata_store import _purge_asset_cache_keep_fingerprint

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
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

            result = _purge_asset_cache_keep_fingerprint(settings)

            cache, _ = _load_rom_metadata_cache(settings)
            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["cleared"]["roms"], 1)
            self.assertEqual(result["cleared"]["preserved_fingerprint"], 1)
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


    def test_bios_files_are_folded_into_systems_tree(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")
        self.assertIn('const BIOS_TREE_ROOT = "__bios__";', source)
        self.assertIn("function selectBiosTreeRoot()", source)
        self.assertIn("function renderBiosTreeFiles()", source)
        self.assertIn("Loading first 10 BIOS files", source)
        self.assertIn("item.bios_md5 || item.md5 || item.fingerprint", source)
        self.assertNotIn("function renderBios()", source)
        self.assertNotIn("function renderBiosList", source)

    def test_integration_transfers_page_consolidates_uploads_and_downloads(self) -> None:
        js = Path(__file__).resolve().parents[1].joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")

        # Downloads and uploads are one consolidated table (direction-tagged rows)
        # under a single Transfers card -- not two separate cards/sections.
        self.assertIn("function renderTransferRows(rows, options = {})", js)
        self.assertIn('row._direction === "upload"', js)
        self.assertNotIn('id="uploadsBody"', js)
        self.assertNotIn("function renderUploadRows", js)
        self.assertNotIn("function renderUploadsPanel", js)
        panel_start = js.index("async function renderIntegrationTransfersPanel(target)")
        panel_end = js.index("async function renderLocalTransferRequestPanel(target)")
        panel_source = js[panel_start:panel_end]
        self.assertEqual(panel_source.count("card log-card"), 1)  # exactly one card, not two
        self.assertIn('api("/admin/downloads")', panel_source)
        self.assertIn('api("/admin/uploads")', panel_source)

        # Per-job pause/resume, wired the same way as the existing cancel/retry.
        self.assertIn("async function pauseDroneDownload(jobId)", js)
        self.assertIn("async function resumeDroneDownload(jobId)", js)
        self.assertIn("/admin/downloads/${encodeURIComponent(jobId)}/pause", js)
        self.assertIn("/admin/downloads/${encodeURIComponent(jobId)}/resume", js)

        # 'pending'/'paused' are real statuses now, not a cosmetic-only relabel of
        # 'queued' (which would now conflate two materially different states).
        self.assertNotIn("usePendingLabel", js)
        render_start = js.index("function renderTransferRows(rows, options = {})")
        render_end = js.index("function renderDownloadsPanel(payload, includeHeader = true)")
        render_source = js[render_start:render_end]
        self.assertIn('"pending", "paused"', render_source)
        self.assertIn("const showActions = options.showActions !== false;", render_source)
        self.assertIn("const assetTableText = options.assetTableText === true;", render_source)
        self.assertIn('class="download-actions">Actions</th>', render_source)
        self.assertIn('showActions ? `<td class="download-actions">${actions}</td>` : ""', render_source)

        recent_start = js.index('<div class="download-section mt-3 mb-0">', render_end)
        recent_end = js.index("</div>`;", recent_start)
        recent_source = js[recent_start:recent_end]
        self.assertIn(
            "renderTransferRows(recentPager.page.rows, { showActions: false, assetTableText: true })",
            recent_source,
        )

    def test_admin_controls_expose_drone_update_actions_and_auto_update(self) -> None:
        api_routes = Path(__file__).resolve().parents[1].joinpath("app/web/api_routes.py").read_text(encoding="utf-8")
        js_source = Path(__file__).resolve().parents[1].joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")
        drone_source = Path(__file__).resolve().parents[1].joinpath("app/common/self_update.py").read_text(encoding="utf-8")
        system_info_source = js_source[
            js_source.index("async function renderAdminSystemInfoPage()"):
            js_source.index("async function renderAdminControlsPage()")
        ]
        controls_source = js_source[
            js_source.index("async function renderAdminControlsPage()"):
            js_source.index("async function loadThemePage(")
        ]

        self.assertIn('parts[1] == "system" and parts[2] == "update-drone"', api_routes)
        self.assertIn('parts[2] == "auto-update"', api_routes)
        self.assertIn('"run-pixn-update", "run-pixen-update"', api_routes)
        self.assertIn("async function updateDroneApp()", js_source)
        self.assertIn('apiPost("/admin/system/update-drone"', js_source)
        self.assertIn('apiPost("/admin/system/auto-update"', js_source)
        self.assertNotIn("Update Drone", system_info_source)
        self.assertNotIn("Run PixN Update", system_info_source)
        self.assertIn("Update Drone", controls_source)
        self.assertIn("Run PixN Update", controls_source)
        self.assertIn("droneAutoUpdateCheckbox", controls_source)
        self.assertIn("DRONE_LATEST_ARCHIVE_URL", drone_source)
        self.assertIn("_schedule_supervised_service_restart", drone_source)
        self.assertIn("start_new_session=True", drone_source)
        self.assertIn("os.execv(sys.executable, [sys.executable, *sys.argv])", drone_source)
        self.assertIn("os._exit(DRONE_SELF_UPDATE_EXIT_CODE)", drone_source)

    def test_drone_self_update_schedules_detached_service_restart_when_supervised(self) -> None:
        from app.common import self_update

        with tempfile.TemporaryDirectory() as tmp:
            bootstrap = Path(tmp) / "service_bootstrap.sh"
            pid_file = Path(tmp) / "drone-server.pid"
            bootstrap.write_text("#!/bin/sh\n", encoding="utf-8")
            pid_file.write_text("123\n", encoding="utf-8")
            with mock.patch.object(self_update, "DRONE_SERVICE_BOOTSTRAP", bootstrap), \
                    mock.patch.object(self_update, "DRONE_SERVICE_PID_FILE", pid_file), \
                    mock.patch.object(self_update.subprocess, "Popen") as popen:
                scheduled = self_update._schedule_supervised_service_restart(1.5)

        self.assertTrue(scheduled)
        command = popen.call_args.args[0]
        self.assertEqual(command[-2:], ["1.5", str(bootstrap)])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_drone_update_overlays_release_files_without_deleting_app_tree(self) -> None:
        drone_source = Path(__file__).resolve().parents[1].joinpath("app/common/self_update.py").read_text(encoding="utf-8")

        self.assertIn("def _overlay_drone_release_tree", drone_source)
        self.assertIn('if "__pycache__" in relative.parts or item.name.endswith(".pyc"):', drone_source)
        self.assertNotIn("shutil.rmtree(target)", drone_source)
        self.assertNotIn("shutil.copy2(item, destination)", drone_source)
        self.assertIn("shutil.copyfile(item, destination)", drone_source)
        self.assertIn("copied_files", drone_source)

    def test_mame_config_source_accepts_batocera_cfg_directory(self) -> None:
        drone_source = Path(__file__).resolve().parents[1].joinpath("app/drone_api.py").read_text(encoding="utf-8") + Path(__file__).resolve().parents[1].joinpath("app/web/handlers_config.py").read_text(encoding="utf-8")

        self.assertIn('"/userdata/system/configs/mame/default.cfg"', drone_source)
        self.assertIn('"/userdata/system/configs/mame"', drone_source)

    def test_route_mixins_export_for_package_startup(self) -> None:
        api_routes = importlib.import_module("app.web.api_routes")
        ui_routes = importlib.import_module("app.web.ui_routes")
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
        drone_source = root.joinpath("app/drone_api.py").read_text(encoding="utf-8") + root.joinpath("app/device/system_info.py").read_text(encoding="utf-8")
        # Service-side logic lives in the versioned bundle (app/service_bootstrap.sh) so new
        # Drone releases apply it automatically; the installed DRONE_SERVER is a thin shim.
        bootstrap = root.joinpath("app/service_bootstrap.sh").read_text(encoding="utf-8")

        # Installer writes a thin shim that ensures the bundle is present and delegates to it.
        self.assertIn("app/service_bootstrap.sh", installer)
        self.assertIn('exec sh "$BOOTSTRAP"', installer)
        self.assertIn("ensure_bundle", installer)
        self.assertIn("DRONE_APP_STAGE_ONLY=1", installer)
        self.assertIn("✓ Updated Drone app bundle", installer)
        self.assertIn("batocera-services enable DRONE_SERVER", installer)
        self.assertIn("batocera-services start DRONE_SERVER", installer)
        self.assertLess(
            installer.index("batocera-services enable DRONE_SERVER"),
            installer.index("batocera-services start DRONE_SERVER"),
        )
        self.assertIn("Starting Drone service so it is ready immediately", installer)

        # All service-side behavior is in the versioned bootstrap.
        self.assertIn("validate_local_app()", bootstrap)
        self.assertIn("missing or empty ${required_file}", bootstrap)
        self.assertIn("Local Drone app import check failed; downloading a fresh app bundle.", bootstrap)
        self.assertIn('"app.web.ui_routes": "UiRoutesMixin"', bootstrap)
        self.assertIn("DRONE_REMOTE_REBOOT_EXIT_CODE", bootstrap)
        self.assertIn("request_host_reboot()", bootstrap)
        self.assertIn("service_control_worker()", bootstrap)
        self.assertIn("/etc/init.d/S31emulationstation stop", bootstrap)
        self.assertIn("/etc/init.d/S31emulationstation start", bootstrap)
        self.assertIn("sleep 3", bootstrap)
        self.assertIn("pidof emulationstation", bootstrap)
        self.assertIn("restart-emulationstation.result", bootstrap)
        self.assertIn("set_screen_mode_as_root()", bootstrap)
        self.assertIn('python3 "$helper" "$mode"', bootstrap)
        self.assertIn("set-screen-mode-${mode}.request", bootstrap)
        self.assertIn("for mode in full kiosk kid", bootstrap)
        self.assertIn("set_volume_as_root()", bootstrap)
        self.assertIn("set-volume.request", bootstrap)
        self.assertIn("stage_latest_app_once()", bootstrap)
        self.assertIn("DRONE_UPDATE_ON_STARTUP", bootstrap)
        self.assertIn("auto-update.enabled", bootstrap)
        self.assertIn('if [ -f "$AUTO_UPDATE_FILE" ]', bootstrap)
        self.assertIn("DRONE_APP_STAGE_ONLY=1", bootstrap)
        self.assertIn('nohup sh "$WORK_DIR/app/service_bootstrap.sh" supervisor', bootstrap)
        self.assertIn("run_supervisor()", bootstrap)
        self.assertIn("service_status()", bootstrap)
        self.assertIn("status)", bootstrap)
        self.assertNotIn('if [ "$exit_code" -eq 0 ]; then\n      exit 0', bootstrap)
        self.assertIn('kill_result="$CONTROL_DIR/kill-emulator.result"', bootstrap)
        self.assertIn("DRONE_SERVICE_CONTROL_DIR", root.joinpath("app/device/device_control.py").read_text(encoding="utf-8"))
        self.assertIn('"screen_mode": _get_screen_mode(settings)', drone_source)
        self.assertIn('"audio_volume": _get_audio_volume(settings)', drone_source)
        self.assertIn("ensure_dns_fallback()", bootstrap)
        self.assertIn("nameserver 1.1.1.1", bootstrap)
        self.assertNotIn("ensure_drone_user", bootstrap)
        self.assertNotIn("DRONE_USER", bootstrap)
        self.assertNotIn("DRONE_GROUP", bootstrap)
        self.assertNotIn("DRONE_REPAIR_ROM_PERMISSIONS", bootstrap)
        self.assertNotIn("run_as_drone", bootstrap)
        self.assertNotIn("su -s /bin/sh -c", installer)
        self.assertIn("bash \"$runner\"", bootstrap)
        self.assertIn("| bash", installer)
        self.assertIn("/userdata/system/drone-app", bootstrap)
        self.assertIn("/userdata/system/logs/drone-app", bootstrap)
        self.assertIn("Drone runs as root and can access Batocera files as root.", installer)
        self.assertIn("does not maintain a read/write allowlist", installer)
        self.assertNotIn("Drone runs as root and can read:", installer)
        self.assertNotIn("Drone runs as root and can write to:", installer)
        self.assertNotIn("/userdata/system/configs/PCSX2/**", installer)
        self.assertNotIn("/userdata/roms/*/gamelist.xml", installer)
        self.assertNotIn("chown root:", bootstrap)
        self.assertNotIn("chmod 2775", bootstrap)
        self.assertNotIn("chmod 664", bootstrap)
        self.assertIn("Drone runs as root; ROM permission repair is not required.", bootstrap)
        self.assertIn("DRONE_UNAUTH_RATE_LIMIT_ENABLED='${DRONE_UNAUTH_RATE_LIMIT_ENABLED:-1}'", bootstrap)
        self.assertIn("DRONE_UNAUTH_RATE_LIMIT_REQUESTS='${DRONE_UNAUTH_RATE_LIMIT_REQUESTS:-60}'", bootstrap)
        self.assertIn("DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS='${DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS:-60}'", bootstrap)
        self.assertIn('DRONE_UNAUTH_RATE_LIMIT_ENABLED="${DRONE_UNAUTH_RATE_LIMIT_ENABLED:-1}"', run_now)
        self.assertIn('DRONE_UNAUTH_RATE_LIMIT_REQUESTS="${DRONE_UNAUTH_RATE_LIMIT_REQUESTS:-60}"', run_now)
        self.assertIn('DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS="${DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS:-60}"', run_now)
        self.assertIn("ROM_METADATA_HASH_ROMS_ENABLED='${ROM_METADATA_HASH_ROMS_ENABLED:-1}'", bootstrap)
        self.assertIn('ROM_METADATA_HASH_ROMS_ENABLED="${ROM_METADATA_HASH_ROMS_ENABLED:-1}"', run_now)
        self.assertIn("ROM_METADATA_UPLOAD_CHUNK_SIZE='${ROM_METADATA_UPLOAD_CHUNK_SIZE:-250}'", bootstrap)
        self.assertIn('ROM_METADATA_UPLOAD_CHUNK_SIZE="${ROM_METADATA_UPLOAD_CHUNK_SIZE:-250}"', run_now)
        self.assertIn("DRONE_LOG_UNAUTHORIZED_REQUESTS='${DRONE_LOG_UNAUTHORIZED_REQUESTS:-0}'", bootstrap)
        self.assertIn('DRONE_LOG_UNAUTHORIZED_REQUESTS="${DRONE_LOG_UNAUTHORIZED_REQUESTS:-0}"', run_now)
        self.assertIn('echo "[drone-service] Downloading and launching Drone app..."\n  wait_for_network', bootstrap)
        self.assertNotIn("ensure_permissions\n    wait_for_network\n\n    supervise_drone", bootstrap)
        self.assertIn("Missing or empty required file", run_now)
        self.assertIn("Downloaded Drone App failed import validation", run_now)
        self.assertIn('"app.web.api_routes": "ApiRoutesMixin"', run_now)
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
        js_source = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")
        css_source = root.joinpath("app/web/static/css/drone.css").read_text(encoding="utf-8")
        api_routes = root.joinpath("app/web/api_routes.py").read_text(encoding="utf-8")
        drone_source = root.joinpath("app/drone_api.py").read_text(encoding="utf-8") + root.joinpath("app/web/handlers_diagnostics.py").read_text(encoding="utf-8")

        self.assertIn("loadSystemInfoBar();", js_source)
        self.assertNotIn("await loadSystemInfoBar();", js_source)
        self.assertIn("getSystemsData(),\n    loadBiosTreeSummary(),", js_source)
        self.assertNotIn("getSystemsData(),\n        refreshRandomThemeLogo(),", js_source)
        self.assertIn('api("/admin/system-info?speed=1")', js_source)
        self.assertIn("include_speed = ", api_routes)
        self.assertIn("def _handle_admin_system_info(self, include_speed: bool = False)", drone_source)
        self.assertIn("_sample_speed() if include_speed else", drone_source)
        self.assertNotIn('id="${prefix}FilterToggle" data-bs-toggle="dropdown"', js_source)
        self.assertIn('event.stopPropagation();', js_source)
        self.assertNotIn("Browse BIOS inside the Systems file tree", js_source)
        self.assertNotIn('class="table table-hover align-middle themed-table bios-table bff-stack"', js_source)
        self.assertIn("Run PixN Update", js_source)
        self.assertIn("admin-config-content", js_source)
        self.assertIn("function buildEmulatorConfigTree", js_source)
        self.assertIn("function toggleEmulatorConfigFolder", js_source)
        self.assertIn("emulator-config-tree", js_source)
        self.assertIn(".tree-grid-row.emulator-tree-row", css_source)
        self.assertIn("var(--tree-depth, 0) * 1.35rem", css_source)
        self.assertIn('const selected = themeFilterInitialized && !(themeFilterSelectedSystems || []).length ? ["__none__"]', js_source)
        self.assertIn('class="system-health-row"', js_source)
        self.assertIn("emulatorConfigSelectionRequestId", js_source)
        self.assertIn("document.activeElement !== versionSelect", js_source)
        self.assertIn('apiPost("/admin/asset-cache/clear-pending"', js_source)
        self.assertIn("What this means:", js_source)

    def test_gpu_info_tolerates_unavailable_detection(self) -> None:
        with mock.patch("app.drone_api.subprocess.run", side_effect=FileNotFoundError()):
            info = _collect_gpu_info()
        self.assertIn("vendor", info)
        self.assertIn("pci_devices", info)

    def test_system_info_includes_performance_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            es_settings = root / "system" / "configs" / "emulationstation" / "es_settings.cfg"
            with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root), "ES_SETTINGS_FILE": str(es_settings)}, clear=True):
                settings = Settings.from_env()
            settings.es_settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings.es_settings_file.write_text('<map><string name="UIMode" value="Kiosk"/></map>', encoding="utf-8")

            with mock.patch("app.device.system_info._get_audio_volume", return_value=55):
                info = _collect_system_info_payload(settings)

            self.assertIn("performance", info)
            self.assertIn("cpu", info["performance"])
            self.assertIn("memory", info["performance"])
            self.assertIn("disk", info["performance"])
            self.assertIn("disks", info["performance"])
            self.assertEqual(info["screen_mode"], "kiosk")
            self.assertEqual(info["audio_volume"], 55)

    def test_mounted_disk_metrics_include_external_drives_without_bind_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            external = Path(tmp) / "media" / "games"
            duplicate = Path(tmp) / "mnt" / "games-bind"
            for path in (root, external, duplicate):
                path.mkdir(parents=True)
            mountinfo = Path(tmp) / "mountinfo"
            mountinfo.write_text(
                "\n".join([
                    f"29 1 8:1 / {root} rw - ext4 /dev/sda1 rw",
                    f"30 1 8:2 / {external} rw - ext4 /dev/sdb1 rw",
                    f"31 1 8:2 / {duplicate} rw - ext4 /dev/sdb1 rw",
                ]),
                encoding="utf-8",
            )
            original_stat = Path.stat

            def fake_stat(path, *args, **kwargs):
                if path.name == "userdata":
                    return mock.Mock(st_dev=101)
                if path.name in {"games", "games-bind"}:
                    return mock.Mock(st_dev=202)
                return original_stat(path, *args, **kwargs)

            with mock.patch("pathlib.Path.stat", autospec=True, side_effect=fake_stat):
                rows = _collect_mounted_disk_metrics(root, mountinfo)

            self.assertEqual(len(rows), 2)
            self.assertTrue(rows[0]["is_main"])
            self.assertEqual(rows[0]["source"], "/dev/sda1")
            self.assertTrue(rows[1]["is_external"])
            self.assertEqual(rows[1]["source"], "/dev/sdb1")

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

    def test_poll_rom_metadata_cache_tolerates_missing_rom_root(self) -> None:
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
            repo = RomRepository(settings.roms_root, settings.bios_root)

            snapshot, _changed, _stats = _poll_rom_metadata_cache(settings, repo)
            self.assertEqual(snapshot["systems"], [])
            self.assertEqual(snapshot["roms"], [])

    def test_poll_rom_metadata_cache_unknown_bios_has_no_systems(self) -> None:
        import app.roms.rom_asset_bios as rom_asset_bios

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            bios = root / "bios" / "unknown.bin"
            bios.parent.mkdir(parents=True)
            bios.write_bytes(b"unrecognized-bios-data")
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)

            with mock.patch.object(rom_asset_bios, "_BIOS_SYSTEM_MAP", {}):
                snapshot, _changed, _stats = _poll_rom_metadata_cache(settings, repo)
            self.assertEqual(snapshot["bios"][0]["systems"], [])

    def test_poll_rom_metadata_cache_attaches_bios_systems_fresh_and_reused(self) -> None:
        import app.roms.rom_asset_bios as rom_asset_bios

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            bios = root / "bios" / "known.bin"
            bios.parent.mkdir(parents=True)
            bios.write_bytes(b"known-bios-data")
            known_md5 = hashlib.md5(b"known-bios-data").hexdigest()
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(settings.roms_root, settings.bios_root)

            with mock.patch.object(rom_asset_bios, "_BIOS_SYSTEM_MAP", {known_md5: ["psx"]}):
                # First poll: BIOS is new, so the md5 gets freshly hashed (rom_scanner's
                # bios_new_or_changed branch) -- systems must be attached there too.
                snapshot, changed, stats = _poll_rom_metadata_cache(settings, repo)
                self.assertTrue(changed)
                self.assertEqual(stats["bios_new_or_changed"], 1)
                self.assertEqual(snapshot["bios"][0]["systems"], ["psx"])
                cache_data, _ = _load_rom_metadata_cache(settings)
                cache_data["dirty"] = False
                _persist_rom_metadata_cache(settings, cache_data)

                # Second poll: file unchanged (same size/mtime) -> md5 is reused, not
                # re-hashed (rom_scanner's reuse branch) -- systems must still be attached.
                with mock.patch.object(RomRepository, "build_md5", side_effect=AssertionError("unchanged BIOS should not re-hash")):
                    snapshot, changed, stats = _poll_rom_metadata_cache(settings, repo)
                self.assertFalse(changed)
                self.assertEqual(snapshot["bios"][0]["systems"], ["psx"])


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

            # Deletes are detected via gamelist.xml (the source of truth): removing the game
            # from the gamelist changes its MD5, so the system re-indexes without the ROM.
            # BIOS is not gamelist-gated, so its filesystem delete is detected directly.
            (rom.parent / "gamelist.xml").write_text("<gameList></gameList>\n", encoding="utf-8")
            rom.unlink()
            bios.unlink()
            snapshot, changed, stats = _poll_rom_metadata_cache(settings, repo)
            self.assertTrue(changed)
            self.assertEqual(stats["deleted"], 1)
            self.assertEqual(stats["bios_deleted"], 1)
            self.assertEqual(snapshot["roms"], [])
            self.assertEqual(snapshot["bios"], [])

    def test_gamelist_edit_reindexes_but_reuses_unchanged_rom_fingerprint(self) -> None:
        # Editing gamelist.xml changes its MD5, so the system re-indexes; but a ROM whose
        # bytes (size) are unchanged reuses its cached fingerprint instead of re-hashing.
        # (Artwork is no longer scanned/stored/uploaded -- resolved live from gamelist for P2P.)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "Game.zip").write_bytes(b"rom-data")
            gamelist = system / "gamelist.xml"
            gamelist.write_text(
                "<gameList><game><path>./Game.zip</path><name>Game</name></game></gameList>\n",
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
                patches = list(_hash_rom_metadata_batches(settings, repo, batch_size=1))
            first_fingerprint = patches[0]["roms"][0]["rom_fingerprint"]
            _mark_rom_metadata_upload_clean(settings)
            # Edit the game's metadata (not the ROM bytes) -> gamelist MD5 changes.
            gamelist.write_text(
                "<gameList><game><path>./Game.zip</path><name>Game (Renamed)</name></game></gameList>\n",
                encoding="utf-8",
            )

            with mock.patch.object(RomRepository, "build_fingerprint", side_effect=AssertionError("unchanged ROM should not re-hash")):
                snapshot, changed, stats = _poll_rom_metadata_cache(settings, repo)

            self.assertTrue(changed)
            self.assertEqual(snapshot["roms"][0]["rom_fingerprint"], first_fingerprint)

    def test_rom_metadata_cache_skips_rescan_when_gamelist_unchanged(self) -> None:
        # Strict gamelist-as-source-of-truth: a ROM file change (here an mtime bump) that is
        # NOT reflected in gamelist.xml triggers no re-scan or re-hash -- the gamelist MD5 is
        # the sole change signal, so the cached fingerprint is preserved untouched.
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

            with mock.patch.object(RomRepository, "build_fingerprint", side_effect=AssertionError("gamelist unchanged -> no re-scan or re-hash")):
                snapshot, changed, stats = _poll_rom_metadata_cache(settings, repo)
                patches = list(_hash_rom_metadata_batches(settings, repo, batch_size=1))

            self.assertFalse(changed)
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

            with mock.patch("app.storage.rom_metadata_store._read_sqlite_rom_metadata_cache", side_effect=sqlite3.OperationalError("unable to open database file")):
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
                "app.roms.rom_scanner._persist_rom_metadata_cache", side_effect=interrupt_after_checkpoint
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
            self.assertEqual(patches[0]["roms"][0]["gamelist_game_id"], "B.zip")

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

    def test_build_rom_metadata_snapshot_rehydrates_current_gamelist_metadata(self) -> None:
        from app.roms.rom_metadata_state import _build_rom_metadata_snapshot_from_cache

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

            # The snapshot is built entirely from the already-scanned cache, re-reading
            # only the gamelist.xml (not a filesystem re-scan), so it reflects the
            # file's current title even though the cache itself was not re-polled.
            cache, _ = _load_rom_metadata_cache(settings)
            result = _build_rom_metadata_snapshot_from_cache(settings, cache, rehydrate_gamelist=True)

            self.assertEqual(result["type"], "asset_metadata")
            self.assertEqual(result["roms"][0]["rom_name"], "Stale XML Title")
            self.assertEqual(result["roms"][0]["gamelist_path"], str((system / "gamelist.xml").resolve()))
            self.assertEqual(result["roms"][0]["gamelist_game_id"], "Game.zip")
            self.assertEqual(result["gamelists"][0]["rom_count"], 1)

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
                    "OVERMIND_DEVICE_ID": "drone-a",
                },
                clear=True,
            ):
                settings = Settings.from_env()

            result = _poll_rom_metadata_once(settings, RomRepository(settings.roms_root, settings.bios_root))

            self.assertEqual(result["status"], "cached")
            self.assertEqual(result["reason"], "scan_complete")
            self.assertEqual(result["hashed_roms"], 1)
            cache, _ = _load_rom_metadata_cache(settings)
            self.assertFalse(cache["dirty"])  # completing the local pass marks the cache clean
            self.assertIn("rom_fingerprint", next(iter(cache["entries"].values())))

    def test_sample_speed_uses_cloudflare_speed_test_endpoints(self) -> None:
            calls = []

            def fake_raw_request(url, data=None):
                calls.append((url, data))
                if data is None:
                    return b"0" * 1000000 if "bytes=1000000" in url else b""
                return b""

            with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
                "app.device.system_metrics._speed_test_raw_request", side_effect=fake_raw_request
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
        ), mock.patch("app.device.system_metrics._speed_test_raw_request", side_effect=URLError("offline")):
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

    def test_list_assets_roms_cache_fast_path_includes_gamelist_artwork(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
                clear=True,
            ):
                settings = Settings.from_env()
            system = root / "roms" / "snes"
            system.mkdir(parents=True)
            (system / "gamelist.xml").write_text(
                """<gameList>
  <game>
    <path>./Game.zip</path>
    <name>Gamelist Game</name>
    <image>./images/Game-image.png</image>
    <marquee>./images/Game-marquee.png</marquee>
  </game>
</gameList>
""",
                encoding="utf-8",
            )
            entries = {
                "snes:Game.zip": {
                    "system": "snes",
                    "file_path": "Game.zip",
                    "rom_name": "Game",
                    "unique_id": "u-game",
                },
            }
            _persist_rom_metadata_cache(
                settings,
                {
                    **_empty_rom_metadata_cache(),
                    "entries": entries,
                    "systems": [{"name": "snes", "rom_count": 1}],
                    "dirty": False,
                    "full_refresh_pending": False,
                    "last_full_scan_at": "2026-06-08T00:00:00Z",
                    "scan_in_progress": False,
                },
                rom_updates=entries,
            )
            repo = RomRepository(settings.roms_root, settings.bios_root, settings=settings)

            _, roms = repo.list_assets("snes", "roms", include_fingerprint=False)

            self.assertEqual(len(roms), 1)
            self.assertEqual(roms[0]["title"], "Gamelist Game")
            self.assertTrue(roms[0]["has_gamelist_entry"])
            self.assertEqual(roms[0]["existing"]["image"], "./images/Game-image.png")
            self.assertEqual(roms[0]["existing"]["marquee"], "./images/Game-marquee.png")

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

    def test_resolve_artwork_file_resolves_video_reference(self) -> None:
        # Video files land in either images/ or videos/ depending on how they
        # arrived (manual upload vs. P2P peer sync -- see
        # test_artwork_video_lands_in_videos_subdir); resolve_artwork_file must
        # follow the actual gamelist reference rather than assuming a folder.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            system_dir = root / "roms" / "snes"
            video_path = system_dir / "videos" / "Chrono Trigger (USA)-video.mp4"
            video_path.parent.mkdir(parents=True, exist_ok=True)
            video_path.write_bytes(b"FAKE-MP4")
            (system_dir / "gamelist.xml").write_text(
                "<gameList><game><path>./Chrono Trigger (USA).zip</path><name>Chrono Trigger</name>"
                "<video>./videos/Chrono Trigger (USA)-video.mp4</video></game></gameList>\n",
                encoding="utf-8",
            )
            repo = RomRepository(root / "roms", root / "bios")
            target, relative_path, artwork_ref = repo.resolve_artwork_file("snes", "Chrono Trigger (USA).zip", "video")
            self.assertEqual(target, video_path.resolve())
            self.assertEqual(relative_path, "videos/Chrono Trigger (USA)-video.mp4")
            self.assertEqual(artwork_ref, "videos/Chrono Trigger (USA)-video.mp4")

    def test_resolve_artwork_file_raises_when_no_video_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            repo = RomRepository(root / "roms", root / "bios")
            with self.assertRaises(FileNotFoundError):
                repo.resolve_artwork_file("snes", "Chrono Trigger (USA).zip", "video")

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

    def test_launchbox_client_uses_current_api_host_first(self) -> None:
        self.assertEqual(LaunchBoxClient().api_bases[0], "https://gamesdb-api.launchbox-app.com/api")

    def test_launchbox_search_supplies_platform_filter(self) -> None:
        urls = []

        class FakeLaunchBoxClient(LaunchBoxClient):
            def _get_json(self, url: str) -> dict:
                urls.append(url)
                return {"data": []}

        FakeLaunchBoxClient().search("Chrono Trigger", system="ps2")
        self.assertTrue(urls)
        self.assertIn("platform=Sony%20Playstation%202", urls[0])

    def test_launchbox_details_imports_metadata_values(self) -> None:
        testcase = self

        class FakeLaunchBoxClient(LaunchBoxClient):
            def _get_json_from_bases(self, path: str, query=None) -> dict:
                testcase.assertEqual(path, "/games/details/123")
                return {
                    "gameKey": 123,
                    "name": "Chrono Trigger",
                    "genres": [{"name": "Role-Playing"}],
                    "developers": [{"name": "Square"}],
                    "publishers": [{"displayName": "Square"}],
                    "players": {"value": "1"},
                    "communityStarRating": 4.8,
                    "gameImages": [],
                }

        details = FakeLaunchBoxClient().details("123")

        self.assertEqual(details["genre"], "Role-Playing")
        self.assertEqual(details["developer"], "Square")
        self.assertEqual(details["publisher"], "Square")
        self.assertEqual(details["players"], "1")
        self.assertEqual(details["rating"], "4.8")

    def test_launchbox_client_sanitizes_dns_failures(self) -> None:
        from app.roms.scrapers import ScraperUnavailableError

        with mock.patch("app.roms.scrapers.urlopen", side_effect=URLError(socket.gaierror(-2, "Name or service not known"))):
            with self.assertRaises(ScraperUnavailableError) as raised:
                LaunchBoxClient().search("Chrono Trigger", system="snes")

        self.assertEqual(str(raised.exception), "LaunchBox could not be reached from this Drone")

    def test_launchbox_search_handler_degrades_when_launchbox_unavailable(self) -> None:
        from app.roms.scrapers import ScraperUnavailableError
        from app.web import handlers_artwork

        class FakeLaunchBoxClient:
            def search(self, query, system=None):
                raise ScraperUnavailableError("LaunchBox could not be reached from this Drone")

        class FakeHandler(handlers_artwork.HandlersArtworkMixin):
            def __init__(self) -> None:
                self.response = None

            def _send_json(self, status_code: int, payload: dict) -> None:
                self.response = (status_code, payload)

        handler = FakeHandler()
        with mock.patch("app.web.handlers_artwork.LaunchBoxClient", FakeLaunchBoxClient):
            handler._handle_admin_launchbox_search("snes", "", "", "Chrono Trigger")

        self.assertIsNotNone(handler.response)
        status_code, payload = handler.response
        self.assertEqual(status_code, 200)
        self.assertTrue(payload["launchbox_unavailable"])
        self.assertEqual(payload["matches"], [])


class DownloadUploadAdminHandlerTests(unittest.TestCase):
    class _FakeHandler:
        def __init__(self, settings) -> None:
            self.settings = settings
            self.response = None

        def _send_json(self, status_code: int, payload: dict) -> None:
            self.response = (status_code, payload)

    def _settings(self, tmp: str):
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
            return Settings.from_env()

    def test_admin_download_pause_and_resume_round_trip(self) -> None:
        from app.web import handlers_downloads

        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            repo = RomRepository(settings.roms_root, settings.bios_root)
            with mock.patch("app.transfer.download_manager.Thread.start"):
                manager = DownloadManager(settings, repo)
            queued = manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "Game.zip")

            class FakeHandler(handlers_downloads.HandlersDownloadsMixin, self._FakeHandler):
                pass

            handler = FakeHandler(settings)
            with mock.patch("app.drone_api._get_download_manager", return_value=manager):
                handler._handle_admin_download_pause(queued["job_id"])
                status_code, payload = handler.response
                self.assertEqual(status_code, 200)
                self.assertEqual(payload["status"], "paused")

                handler._handle_admin_download_pause("does-not-exist")
                self.assertEqual(handler.response[0], 404)

                handler._handle_admin_download_resume(queued["job_id"])
                status_code, payload = handler.response
                self.assertEqual(status_code, 200)
                self.assertEqual(payload["status"], "queued")

                handler._handle_admin_download_resume(queued["job_id"])
                self.assertEqual(handler.response[0], 409)  # already queued, not paused

    def test_admin_uploads_endpoint_returns_tracker_snapshot(self) -> None:
        from app.transfer.upload_tracker import UploadTracker
        from app.web import handlers_downloads

        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            tracker = UploadTracker()
            tracker.start(peer_device_id="peer-a", asset_type="rom", relative_path="Game.zip", total_bytes=100)

            class FakeHandler(handlers_downloads.HandlersDownloadsMixin, self._FakeHandler):
                pass

            handler = FakeHandler(settings)
            with mock.patch("app.web.handlers_downloads._get_upload_tracker", return_value=tracker):
                handler._handle_admin_uploads()
            status_code, payload = handler.response
            self.assertEqual(status_code, 200)
            self.assertEqual(payload["target_drone_id"], "target-a")
            self.assertEqual(len(payload["active"]), 1)
            self.assertEqual(payload["active"][0]["peer_device_id"], "peer-a")
            self.assertEqual(payload["recent"], [])


class UploadTrackerTests(unittest.TestCase):
    def test_start_progress_finish_lifecycle(self) -> None:
        from app.transfer.upload_tracker import UploadTracker

        tracker = UploadTracker()
        upload_id = tracker.start(peer_device_id="peer-a", asset_type="rom", relative_path="snes/Game.zip", system="snes", total_bytes=1000)
        snapshot = tracker.snapshot()
        self.assertEqual(len(snapshot["active"]), 1)
        self.assertEqual(snapshot["active"][0]["status"], "uploading")
        self.assertEqual(snapshot["active"][0]["file_name"], "Game.zip")

        tracker.progress(upload_id, 500)
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["active"][0]["bytes_transferred"], 500)
        self.assertEqual(snapshot["active"][0]["percentage"], 50.0)

        tracker.finish(upload_id, "completed")
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["active"], [])
        self.assertEqual(len(snapshot["recent"]), 1)
        self.assertEqual(snapshot["recent"][0]["status"], "completed")

    def test_finish_with_error_marks_failed(self) -> None:
        from app.transfer.upload_tracker import UploadTracker

        tracker = UploadTracker()
        upload_id = tracker.start(peer_device_id=None, asset_type="bios", relative_path="bios.bin")
        tracker.finish(upload_id, "failed", error="peer disconnected")
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["recent"][0]["status"], "failed")
        self.assertEqual(snapshot["recent"][0]["error_message"], "peer disconnected")
        self.assertIsNone(snapshot["recent"][0]["peer_device_id"])

    def test_recent_list_is_bounded(self) -> None:
        from app.transfer.upload_tracker import UPLOAD_RECENT_LIMIT, UploadTracker

        tracker = UploadTracker()
        for index in range(UPLOAD_RECENT_LIMIT + 5):
            upload_id = tracker.start(peer_device_id="peer-a", asset_type="rom", relative_path=f"Game{index}.zip")
            tracker.finish(upload_id, "completed")
        snapshot = tracker.snapshot()
        self.assertEqual(len(snapshot["recent"]), UPLOAD_RECENT_LIMIT)
        # Most recently finished first.
        self.assertEqual(snapshot["recent"][0]["relative_path"], f"Game{UPLOAD_RECENT_LIMIT + 4}.zip")


if __name__ == "__main__":
    unittest.main()


class DroneServerErrorHandlingTests(unittest.TestCase):
    def test_handle_error_logs_concise_line_for_tls_probe(self) -> None:
        import contextlib
        import ssl as _ssl
        from unittest import mock as _mock

        captured = io.StringIO()
        try:
            raise _ssl.SSLError("UNEXPECTED_RECORD")
        except _ssl.SSLError:
            with contextlib.redirect_stderr(captured):
                # Bound-method call with a stand-in self; the SSLError branch returns
                # before any super() call, so a Mock self is sufficient.
                DroneThreadingHTTPServer.handle_error(_mock.Mock(), object(), ("66.228.34.203", 4444))
        output = captured.getvalue()
        self.assertIn("Dropped untrusted/insecure connection from 66.228.34.203", output)
        self.assertIn("SSLError", output)
        self.assertNotIn("Traceback", output)


class DroneTlsHandshakeTests(unittest.TestCase):
    def test_request_handler_has_idle_timeout(self) -> None:
        from app.drone_api import RomRequestHandler
        # Per-connection timeout must be set so a stalled/silent client cannot hold a
        # worker thread (or, before this fix, wedge accept()) forever.
        self.assertIsNotNone(RomRequestHandler.timeout)
        self.assertGreaterEqual(RomRequestHandler.timeout, 15)

    def test_apply_server_tls_defers_handshake_off_accept_loop(self) -> None:
        from unittest import mock as _mock
        import app.drone_api as da

        # The fixture settings are otherwise Mocks, but local-network cert
        # loading is unconditional now, so userdata_root must be a real
        # (empty) directory for paired_peers()/local cert cache lookups.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        settings = _mock.Mock()
        settings.userdata_root = Path(self._tmp.name)
        settings.http_only = False
        settings.drone_mtls_mode = "self-signed"
        settings.drone_mtls_enabled = False
        settings.drone_mtls_ca_file = None
        cert = _mock.Mock(); cert.exists.return_value = True
        key = _mock.Mock(); key.exists.return_value = True
        settings.drone_cert_file = cert
        settings.drone_key_file = key

        ctx = _mock.Mock()
        ctx.wrap_socket.return_value = "wrapped"
        server = _mock.Mock()
        server.socket = "raw"
        with _mock.patch("app.drone_api.ssl.SSLContext", return_value=ctx):
            da._apply_server_tls(settings, server)

        # The listening socket must be wrapped with do_handshake_on_connect=False so the
        # TLS handshake never runs on the single accept thread.
        _, kwargs = ctx.wrap_socket.call_args
        self.assertFalse(kwargs.get("do_handshake_on_connect"))
        self.assertTrue(kwargs.get("server_side"))
        self.assertEqual(server.socket, "wrapped")

    def test_apply_server_tls_skips_empty_peer_certificate_path(self) -> None:
        # Regression: a paired peer whose certificate_path is empty/blank/"." must be
        # skipped. Path("") == Path(".") *exists* as a directory, so the old exists()
        # check let such an entry reach load_verify_locations(cafile="."), which raises
        # IsADirectoryError -- an OSError the ssl.SSLError handler did NOT catch, so it
        # crashed drone startup. The empty-string + is_file() guards must skip these.
        from unittest import mock as _mock
        import app.drone_api as da

        settings = _mock.Mock()
        settings.http_only = False
        settings.drone_mtls_mode = "self-signed"
        settings.drone_mtls_enabled = True
        settings.drone_mtls_ca_file = None
        cert = _mock.Mock(); cert.exists.return_value = True
        key = _mock.Mock(); key.exists.return_value = True
        settings.drone_cert_file = cert
        settings.drone_key_file = key

        ctx = _mock.Mock()
        ctx.wrap_socket.return_value = "wrapped"
        server = _mock.Mock()
        server.socket = "raw"

        bogus_peers = [
            {"certificate_path": ""},
            {"certificate_path": "   "},
            {"certificate_path": "."},
            {"certificate_path": None},
            {},  # certificate_path missing entirely
        ]
        with tempfile.TemporaryDirectory() as tmp, \
                _mock.patch("app.drone_api.ssl.SSLContext", return_value=ctx), \
                _mock.patch.object(da._local_network, "is_local_mode", return_value=False), \
                _mock.patch.object(da._local_network, "paired_peers", return_value=bogus_peers), \
                _mock.patch("app.drone_api._local_peer_cert_cache_path",
                            return_value=Path(tmp) / "nope" / "x"):
            # Must not raise (previously IsADirectoryError on cafile=".").
            da._apply_server_tls(settings, server)

        # No bogus path may reach load_verify_locations -- the "." case is the crash.
        loaded = [c.kwargs.get("cafile") for c in ctx.load_verify_locations.call_args_list]
        self.assertNotIn(".", loaded)
        self.assertNotIn("", loaded)
        self.assertEqual(ctx.load_verify_locations.call_count, 0)
        self.assertEqual(server.socket, "wrapped")

    def test_apply_server_tls_survives_oserror_loading_peer_cert(self) -> None:
        # The peer-cert load is guarded by (ssl.SSLError, OSError): a paired peer cert
        # that exists as a real file but is unreadable/garbage (raising OSError such as
        # IsADirectoryError/PermissionError from load_verify_locations) must be skipped,
        # not crash startup.
        from unittest import mock as _mock
        import app.drone_api as da

        settings = _mock.Mock()
        settings.http_only = False
        settings.drone_mtls_mode = "self-signed"
        settings.drone_mtls_enabled = True
        settings.drone_mtls_ca_file = None
        cert = _mock.Mock(); cert.exists.return_value = True
        key = _mock.Mock(); key.exists.return_value = True
        settings.drone_cert_file = cert
        settings.drone_key_file = key

        ctx = _mock.Mock()
        ctx.wrap_socket.return_value = "wrapped"
        ctx.load_verify_locations.side_effect = OSError("unreadable cert")
        server = _mock.Mock()
        server.socket = "raw"

        with tempfile.TemporaryDirectory() as tmp:
            peer_cert = Path(tmp) / "peer.crt"
            peer_cert.write_text("not-a-real-cert")
            with _mock.patch("app.drone_api.ssl.SSLContext", return_value=ctx), \
                    _mock.patch.object(da._local_network, "is_local_mode", return_value=False), \
                    _mock.patch.object(da._local_network, "paired_peers",
                                       return_value=[{"certificate_path": str(peer_cert)}]), \
                    _mock.patch("app.drone_api._local_peer_cert_cache_path",
                                return_value=Path(tmp) / "nope" / "x"):
                # Must not raise even though load_verify_locations raises OSError.
                da._apply_server_tls(settings, server)

        # The real file passed is_file() so a load was attempted, but the OSError was
        # swallowed rather than propagated.
        self.assertGreaterEqual(ctx.load_verify_locations.call_count, 1)
        self.assertEqual(server.socket, "wrapped")


class LocalNetworkAssetCopyTests(unittest.TestCase):
    """Local Network 'Request Assets' panel: system-optional browse, paging,
    and copying ROMs together with their gamelist-referenced artwork."""

    def _settings(self, root):
        with mock.patch.dict(
            "os.environ",
            {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "OVERMIND_DEVICE_ID": "target-a",
            },
            clear=True,
        ):
            return drone_api.Settings.from_env()

    def _seed_two_systems(self, root):
        roms = root / "roms"
        (roms / "snes").mkdir(parents=True)
        (roms / "gba").mkdir(parents=True)
        (roms / "snes" / "Super Mario World.zip").write_bytes(b"smw-rom-bytes")
        (roms / "gba" / "Metroid.zip").write_bytes(b"metroid-rom-bytes")
        images = roms / "snes" / "images"
        images.mkdir()
        (images / "Super Mario World.png").write_bytes(b"box-art")
        (roms / "snes" / "gamelist.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<gameList>\n'
            "  <game>\n"
            "    <path>./Super Mario World.zip</path>\n"
            "    <name>Super Mario World</name>\n"
            "    <image>./images/Super Mario World.png</image>\n"
            "    <marquee>./images/Super Mario World.png</marquee>\n"
            "  </game>\n"
            "</gameList>\n",
            encoding="utf-8",
        )

    def _handler(self, settings, repo):
        handler = object.__new__(drone_api.RomRequestHandler)
        handler.settings = settings
        handler.repository = repo
        return handler

    def test_collect_peer_inventory_roms_without_system_spans_all_systems(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)

            payload = handler._collect_peer_inventory("roms", {})
            self.assertEqual(payload["total"], 2)
            systems = {item.get("system") for item in payload["items"]}
            self.assertEqual(systems, {"snes", "gba"})

            # Paging across the combined library.
            page = handler._collect_peer_inventory("roms", {"limit": ["1"], "offset": ["0"]})
            self.assertEqual(page["total"], 2)
            self.assertEqual(len(page["items"]), 1)
            page2 = handler._collect_peer_inventory("roms", {"limit": ["1"], "offset": ["1"]})
            self.assertEqual(len(page2["items"]), 1)
            self.assertNotEqual(page["items"][0]["system"], page2["items"][0]["system"])

    def test_collect_peer_inventory_roms_pages_in_sql_before_decoding(self):
        from app.storage import rom_metadata_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = self._settings(root)
            entries = {
                f"{system}:game-{index}.zip": {
                    "system": system,
                    "file_path": f"game-{index}.zip",
                    "rom_name": f"{system} Game {index:03d}",
                    "unique_id": f"{system}-{index}",
                    "file_size": index + 1,
                    "fingerprint": f"fp-{system}-{index}",
                }
                for system in ("snes", "gba", "genesis")
                for index in range(100)
            }
            _persist_rom_metadata_cache(
                settings,
                {
                    **_empty_rom_metadata_cache(),
                    "entries": entries,
                    "systems": [
                        {"name": system, "rom_count": 100}
                        for system in ("snes", "gba", "genesis")
                    ],
                    "last_full_scan_at": "2026-07-14T00:00:00Z",
                    "scan_in_progress": False,
                },
                rom_updates=entries,
            )
            repo = drone_api.RomRepository(settings.roms_root, settings.bios_root, settings=settings)
            handler = self._handler(settings, repo)
            original_decoder = rom_metadata_store._rom_row_payload
            with mock.patch.object(
                repo,
                "list_assets",
                side_effect=AssertionError("paged inventory must not load complete systems"),
            ), mock.patch.object(
                rom_metadata_store,
                "_rom_row_payload",
                wraps=original_decoder,
            ) as decoded:
                payload = handler._collect_peer_inventory(
                    "roms",
                    {"limit": ["50"], "offset": ["0"]},
                )

            self.assertEqual(payload["total"], 300)
            self.assertEqual(len(payload["items"]), 50)
            self.assertEqual(decoded.call_count, 50)
            self.assertEqual(
                {item["system"] for item in payload["items"][:3]},
                {"snes", "gba", "genesis"},
            )
            with open_database(database_path(root)) as connection:
                plan = connection.execute(
                    "EXPLAIN QUERY PLAN SELECT entry_key FROM rom_cache_entries "
                    "WHERE system = ? COLLATE NOCASE "
                    "ORDER BY rom_name COLLATE NOCASE, file_path COLLATE NOCASE, entry_key LIMIT ? OFFSET ?",
                    ("snes", 50, 0),
                ).fetchall()
            self.assertTrue(any("idx_rom_cache_page" in str(row[3]) for row in plan))

    def test_peer_bios_artwork_and_saves_inventories_page_in_sql(self):
        from app.storage import saves_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = self._settings(root)
            bios = {
                "bios:a": {"file_path": "ps2/a.bin", "name": "A BIOS", "md5": "a" * 32, "systems": ["ps2"]},
                "bios:b": {"file_path": "root.bin", "name": "B BIOS", "md5": "b" * 32, "systems": []},
            }
            artwork = {
                "art:snes:a": {"system": "snes", "rom_path": "a.zip", "title": "A", "artwork_types": ["image"]},
                "art:gba:b": {"system": "gba", "rom_path": "b.zip", "title": "B", "artwork_types": ["marquee"]},
            }
            _persist_rom_metadata_cache(
                settings,
                {
                    **_empty_rom_metadata_cache(),
                    "bios_entries": bios,
                    "artwork_entries": artwork,
                    "last_full_scan_at": "2026-07-14T00:00:00Z",
                    "scan_in_progress": False,
                },
                bios_updates=bios,
                artwork_updates=artwork,
            )
            settings.saves_root.mkdir(parents=True, exist_ok=True)
            with saves_store._open(settings.saves_root) as connection:
                connection.executemany(
                    "INSERT INTO saves_cache_entries "
                    "(entry_key, system, file_path, save_name, absolute_path, file_size, modified_time, fingerprint) "
                    "VALUES (?, ?, ?, ?, '', ?, ?, ?)",
                    [
                        ("save-a", "snes", "snes/a.srm", "a.srm", 10, 1, "save-fp-a"),
                        ("save-b", "gba", "gba/b.sav", "b.sav", 20, 2, "save-fp-b"),
                    ],
                )
            repo = drone_api.RomRepository(settings.roms_root, settings.bios_root, settings=settings)
            handler = self._handler(settings, repo)
            with mock.patch.object(repo, "list_bios_entries", side_effect=AssertionError("must use BIOS SQL page")), \
                    mock.patch.object(repo, "list_artwork_metadata", side_effect=AssertionError("must use artwork SQL page")), \
                    mock.patch.object(saves_store, "list_saves", side_effect=AssertionError("must use saves SQL page")):
                bios_page = handler._collect_peer_inventory("bios", {"limit": ["1"], "offset": ["1"]})
                artwork_page = handler._collect_peer_inventory(
                    "artwork",
                    {"systems": ["gba"], "limit": ["1"], "offset": ["0"]},
                )
                saves_page = handler._collect_peer_inventory("saves", {"limit": ["1"], "offset": ["0"]})

            self.assertEqual((bios_page["total"], len(bios_page["items"])), (2, 1))
            self.assertEqual(artwork_page["total"], 1)
            self.assertEqual(artwork_page["items"][0]["system"], "gba")
            self.assertEqual((saves_page["total"], len(saves_page["items"])), (2, 1))

    def test_local_rom_and_bios_handlers_use_database_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = self._settings(root)
            roms = {
                f"snes:game-{index}.zip": {
                    "system": "snes",
                    "file_path": f"game-{index}.zip",
                    "rom_name": f"Game {index:03d}",
                    "file_size": index + 1,
                }
                for index in range(10)
            }
            bios = {
                "bios:assigned": {
                    "file_path": "ps2/assigned.bin",
                    "name": "Assigned",
                    "systems": ["ps2"],
                },
                "bios:shared": {
                    "file_path": "shared.bin",
                    "name": "Shared",
                    "systems": ["ps2", "ps3"],
                },
            }
            _persist_rom_metadata_cache(
                settings,
                {
                    **_empty_rom_metadata_cache(),
                    "entries": roms,
                    "bios_entries": bios,
                    "last_full_scan_at": "2026-07-14T00:00:00Z",
                    "scan_in_progress": False,
                },
                rom_updates=roms,
                bios_updates=bios,
            )
            repo = drone_api.RomRepository(settings.roms_root, settings.bios_root, settings=settings)
            handler = self._handler(settings, repo)
            with mock.patch.object(repo, "list_assets", side_effect=AssertionError("must use ROM SQL page")), \
                    mock.patch.object(repo, "list_bios_entries", side_effect=AssertionError("must use BIOS SQL page")), \
                    mock.patch.object(handler, "_send_json") as send:
                handler._handle_rom_list("snes", limit=3, offset=4)
                rom_payload = send.call_args.args[1]
                handler._handle_bios_list(limit=1, offset=0, unassigned=True)
                bios_payload = send.call_args.args[1]

            self.assertEqual((rom_payload["count"], rom_payload["returned"]), (10, 3))
            self.assertEqual((bios_payload["count"], bios_payload["returned"]), (1, 1))
            self.assertEqual(bios_payload["bios"][0]["name"], "Shared")

    def test_peer_page_local_exists_annotation_uses_indexed_cache_lookups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = self._settings(root)
            entries = {
                "snes:local.zip": {
                    "system": "snes",
                    "file_path": "local.zip",
                    "rom_name": "Local",
                    "file_size": 10,
                    "fingerprint": "same-fingerprint",
                }
            }
            _persist_rom_metadata_cache(
                settings,
                {
                    **_empty_rom_metadata_cache(),
                    "entries": entries,
                    "last_full_scan_at": "2026-07-14T00:00:00Z",
                    "scan_in_progress": False,
                },
                rom_updates=entries,
            )
            repo = drone_api.RomRepository(settings.roms_root, settings.bios_root, settings=settings)
            handler = self._handler(settings, repo)
            items = [
                {"system": "snes", "relative_path": "different-name.zip", "fingerprint": "same-fingerprint"},
                {"system": "snes", "relative_path": "missing.zip", "fingerprint": "not-present"},
            ]
            with mock.patch.object(
                handler,
                "_local_rom_index",
                side_effect=AssertionError("paged annotation must not build full local indexes"),
            ):
                handler._annotate_roms_exist_locally(items)
            self.assertTrue(items[0]["exists_locally"])
            self.assertFalse(items[1]["exists_locally"])

    def test_gameplay_history_migrates_to_indexed_paged_rows(self):
        from app.device.game_activity import (
            GAMEPLAY_HISTORY_NAMESPACE,
            load_gameplay_history_page,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = self._settings(root)
            history = [
                {
                    "played_at": f"2026-07-14T0{index}:00:00+00:00",
                    "system_name": "snes",
                    "game_name": f"Game {index}",
                    "rom_path": f"/userdata/roms/snes/game-{index}.zip",
                }
                for index in range(3)
            ]
            save_payload(database_path(root), GAMEPLAY_HISTORY_NAMESPACE, history)
            page = load_gameplay_history_page(settings, limit=1, offset=1)
            self.assertEqual((page["total"], len(page["items"])), (3, 1))
            self.assertEqual(page["items"][0]["game_name"], "Game 1")
            with open_database(database_path(root)) as connection:
                count = connection.execute("SELECT COUNT(*) FROM gameplay_history").fetchone()[0]
                indexes = {row[1] for row in connection.execute("PRAGMA index_list(gameplay_history)")}
            self.assertEqual(count, 3)
            self.assertIn("idx_gameplay_history_played_at", indexes)

    def test_collect_peer_inventory_roms_with_system_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)

            payload = handler._collect_peer_inventory("roms", {"system": ["gba"]})
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["items"][0]["system"], "gba")

    def test_collect_peer_inventory_roms_with_systems_plural_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)

            # The Local Network UI sends ?systems=<csv>; only those systems' ROMs
            # come back (and we don't scan the whole library to do it).
            single = handler._collect_peer_inventory("roms", {"systems": ["gba"]})
            self.assertEqual({item["system"] for item in single["items"]}, {"gba"})
            self.assertEqual(single["total"], 1)

            both = handler._collect_peer_inventory("roms", {"systems": ["snes,gba"]})
            self.assertEqual({item["system"] for item in both["items"]}, {"snes", "gba"})

    def test_collect_peer_inventory_roms_interleaves_multiple_systems(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            # Several ROMs per system so ordering is observable.
            for index in range(5):
                (root / "roms" / "snes" / f"snes-game-{index}.zip").write_bytes(b"s")
                (root / "roms" / "gba" / f"gba-game-{index}.zip").write_bytes(b"g")
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)

            payload = handler._collect_peer_inventory("roms", {"systems": ["snes,gba"]})
            order = [item["system"] for item in payload["items"]]
            self.assertEqual(set(order), {"snes", "gba"})
            # Interleaved, not grouped: both systems appear within the first few
            # items (a grouped result would be all of one system first, which is
            # what made multi-system requests look like only one system).
            self.assertEqual(set(order[:4]), {"snes", "gba"})

    def _peer_only_rom_item(self):
        # A ROM that is NOT on the local machine (fingerprint not in the seeded
        # snes library), carrying gamelist artwork fields.
        return {
            "system": "snes",
            "relative_path": "Peer Only Game.zip",
            "rom_path": "Peer Only Game.zip",
            "rom_fingerprint": "ffffffffffffffffffffffffffffffff",
            "gamelist": {"image": "./images/Peer Only Game.png", "marquee": "./images/Peer Only Game.png"},
        }

    def test_enqueue_local_asset_new_rom_includes_gamelist_artwork(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)
            peer = {"drone_id": "source-a", "reachable_url": "http://source-a:8080"}

            jobs = handler._enqueue_local_asset(
                manager, {}, peer, "roms", self._peer_only_rom_item(), default_system="snes", include_artwork=True
            )
            self.assertEqual(jobs[0]["file_type"], "ROM")
            artwork = [j for j in jobs if j.get("file_type") == "ARTWORK"]
            self.assertEqual(sorted(j["artwork_type"] for j in artwork), ["image", "marquee"])

    def test_enqueue_local_asset_new_rom_without_artwork(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)

            jobs = handler._enqueue_local_asset(
                manager, {}, {"drone_id": "source-a"}, "roms", self._peer_only_rom_item(),
                default_system="snes", include_artwork=False,
            )
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["file_type"], "ROM")

    def test_existing_rom_is_skipped_but_artwork_still_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)

            # An item identical to the local ROM (same fingerprint) -> "exists".
            inventory = handler._collect_peer_inventory("roms", {"system": ["snes"]})
            local_item = inventory["items"][0]
            self.assertTrue(handler._match_local_rom(handler._local_rom_index("snes"), local_item))

            jobs = handler._enqueue_local_asset(
                manager, {}, {"drone_id": "source-a"}, "roms", local_item,
                default_system="snes", include_artwork=True,
            )
            # ROM not re-downloaded, but its artwork is still queued.
            self.assertFalse(any(j.get("file_type") == "ROM" for j in jobs))
            artwork = [j for j in jobs if j.get("file_type") == "ARTWORK"]
            self.assertEqual(sorted(j["artwork_type"] for j in artwork), ["image", "marquee"])

    def test_existing_artwork_is_skipped_when_not_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)

            inventory = handler._collect_peer_inventory("roms", {"system": ["snes"]})
            local_item = inventory["items"][0]

            # ROM already present AND both artwork files already on disk: with
            # overwrite disabled, nothing is queued.
            jobs = handler._enqueue_local_asset(
                manager, {}, {"drone_id": "source-a"}, "roms", local_item,
                default_system="snes", include_artwork=True, overwrite_artwork=False,
            )
            self.assertEqual(jobs, [])

    def test_only_missing_artwork_is_fetched_when_not_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            # image -> existing file (present); marquee -> a file not on disk (missing).
            (root / "roms" / "snes" / "gamelist.xml").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n<gameList>\n'
                "  <game>\n"
                "    <path>./Super Mario World.zip</path>\n"
                "    <name>Super Mario World</name>\n"
                "    <image>./images/Super Mario World.png</image>\n"
                "    <marquee>./images/Super Mario World Marquee.png</marquee>\n"
                "  </game>\n"
                "</gameList>\n",
                encoding="utf-8",
            )
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)

            inventory = handler._collect_peer_inventory("roms", {"system": ["snes"]})
            local_item = inventory["items"][0]

            jobs = handler._enqueue_local_asset(
                manager, {}, {"drone_id": "source-a"}, "roms", local_item,
                default_system="snes", include_artwork=True, overwrite_artwork=False,
            )
            # Only the missing marquee is fetched; the already-present image is left alone.
            artwork = [j for j in jobs if j.get("file_type") == "ARTWORK"]
            self.assertEqual(sorted(j["artwork_type"] for j in artwork), ["marquee"])

    def test_artwork_only_copies_artwork_for_existing_rom_without_the_rom(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)

            inventory = handler._collect_peer_inventory("roms", {"system": ["snes"]})
            local_item = inventory["items"][0]  # matches the seeded local ROM

            jobs = handler._enqueue_local_asset(
                manager, {}, {"drone_id": "source-a"}, "roms", local_item,
                default_system="snes", artwork_only=True, overwrite_artwork=True,
            )
            # No ROM file is ever queued; only its artwork is.
            self.assertFalse(any(j.get("file_type") == "ROM" for j in jobs))
            artwork = [j for j in jobs if j.get("file_type") == "ARTWORK"]
            self.assertEqual(sorted(j["artwork_type"] for j in artwork), ["image", "marquee"])

    def test_include_roms_disabled_copies_only_artwork_for_existing_rom(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)

            local_item = handler._collect_peer_inventory("roms", {"system": ["snes"]})["items"][0]
            jobs = handler._enqueue_local_asset(
                manager, {}, {"drone_id": "source-a"}, "roms", local_item,
                default_system="snes", include_artwork=True, include_roms=False,
                overwrite_files=True,
            )

            self.assertFalse(any(job.get("file_type") == "ROM" for job in jobs))
            self.assertEqual(
                sorted(job["artwork_type"] for job in jobs if job.get("file_type") == "ARTWORK"),
                ["image", "marquee"],
            )

    def test_overwrite_files_queues_an_existing_rom_for_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)

            local_item = handler._collect_peer_inventory("roms", {"system": ["snes"]})["items"][0]
            jobs = handler._enqueue_local_asset(
                manager, {}, {"drone_id": "source-a"}, "roms", local_item,
                default_system="snes", include_artwork=False, include_roms=True,
                overwrite_files=True,
            )

            rom_job = next(job for job in jobs if job.get("file_type") == "ROM")
            self.assertTrue(manager._jobs[rom_job["job_id"]]["_overwrite"])

    def test_artwork_only_skips_rom_not_present_on_this_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)

            # A peer ROM that is NOT on this machine -> artwork-only must do nothing
            # (neither the ROM nor its artwork is queued).
            jobs = handler._enqueue_local_asset(
                manager, {}, {"drone_id": "source-a"}, "roms", self._peer_only_rom_item(),
                default_system="snes", artwork_only=True,
            )
            self.assertEqual(jobs, [])
            self.assertEqual(manager.snapshot()["queued"], [])

    def test_artwork_only_respects_overwrite_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)

            inventory = handler._collect_peer_inventory("roms", {"system": ["snes"]})
            local_item = inventory["items"][0]

            # Existing ROM whose image+marquee are already present on disk: artwork-only
            # with overwrite disabled has nothing to do.
            jobs = handler._enqueue_local_asset(
                manager, {}, {"drone_id": "source-a"}, "roms", local_item,
                default_system="snes", artwork_only=True, overwrite_artwork=False,
            )
            self.assertEqual(jobs, [])

    def test_existing_rom_on_disk_is_skipped_even_when_metadata_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)
            item = {
                "system": "snes",
                "relative_path": "Super Mario World.zip",
                "file_size": (root / "roms" / "snes" / "Super Mario World.zip").stat().st_size,
            }

            with mock.patch.object(repo, "list_assets", return_value=(root / "roms" / "snes", [])):
                jobs = handler._enqueue_local_asset(
                    manager, {}, {"drone_id": "source-a"}, "roms", item,
                    default_system="snes", include_artwork=False,
                )

            self.assertEqual(jobs, [])
            self.assertEqual(manager.snapshot()["queued"], [])

    def test_existing_rom_fingerprint_is_skipped_even_when_peer_name_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)
            local_rom = root / "roms" / "snes" / "Super Mario World.zip"
            item = {
                "system": "snes",
                "relative_path": "Super Mario World (USA).zip",
                "file_size": local_rom.stat().st_size,
                "rom_fingerprint": repo.build_fingerprint(local_rom),
            }
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)

            jobs = handler._enqueue_local_asset(
                manager, {}, {"drone_id": "source-a"}, "roms", item,
                default_system="snes", include_artwork=False,
            )

            self.assertEqual(jobs, [])
            self.assertEqual(manager.snapshot()["queued"], [])

    def test_duplicate_rom_queue_request_is_not_enqueued_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)
            peer = {"drone_id": "source-a", "reachable_url": "http://source-a:8080"}

            first = handler._enqueue_local_asset(
                manager, {}, peer, "roms", self._peer_only_rom_item(),
                default_system="snes", include_artwork=False,
            )
            second = handler._enqueue_local_asset(
                manager, {}, peer, "roms", self._peer_only_rom_item(),
                default_system="snes", include_artwork=False,
            )

            self.assertEqual(len([job for job in first if job.get("file_type") == "ROM"]), 1)
            self.assertEqual(second, [])
            self.assertEqual(len(manager.snapshot()["queued"]), 1)

    def test_annotate_roms_exist_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            handler = self._handler(settings, repo)

            inventory = handler._collect_peer_inventory("roms", {"system": ["snes"]})
            items = inventory["items"] + [self._peer_only_rom_item()]
            handler._annotate_roms_exist_locally(items)
            self.assertTrue(items[0]["exists_locally"])      # the seeded local ROM
            self.assertFalse(items[-1]["exists_locally"])    # the peer-only ROM

    def test_artwork_overwrite_names_by_local_rom_and_updates_gamelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            peer = {"drone_id": "src", "reachable_url": "http://src:8080"}

            class FakeResponse:
                def __init__(self, data, name="images/SomethingElse-image.png"):
                    self._chunks = [data, b""]
                    self.headers = {"X-Asset-Relative-Path": name, "Content-Length": str(len(data))}

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self, _size=-1):
                    return self._chunks.pop(0)

                def geturl(self):
                    return "http://src:8080/v1/api/peer/artwork/snes/image/Super%20Mario%20World.zip"

            with mock.patch("app.transfer.peer_download.urlopen", return_value=FakeResponse(b"NEWART")):
                result = drone_api._download_artwork_from_peer(
                    settings, repo, {}, peer, "snes", "Super Mario World.zip", "image",
                    overwrite=True, local_rom_path="Super Mario World.zip",
                )

            self.assertEqual(result["status"], "completed")
            # Named by the local ROM stem + field, regardless of the peer's own name.
            target = root / "roms" / "snes" / "images" / "Super Mario World-image.png"
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"NEWART")
            self.assertEqual(result["gamelist_update_status"], "succeeded")
            gamelist = (root / "roms" / "snes" / "gamelist.xml").read_text(encoding="utf-8")
            self.assertIn("images/Super Mario World-image.png", gamelist)

            # A second copy overwrites the same file (no "-1" duplicate).
            with mock.patch("app.transfer.peer_download.urlopen", return_value=FakeResponse(b"NEWER!")):
                drone_api._download_artwork_from_peer(
                    settings, repo, {}, peer, "snes", "Super Mario World.zip", "image",
                    overwrite=True, local_rom_path="Super Mario World.zip",
                )
            self.assertEqual(target.read_bytes(), b"NEWER!")
            self.assertFalse((root / "roms" / "snes" / "images" / "Super Mario World-image-1.png").exists())

    def test_ensure_rom_write_access_returns_true_on_ok_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "control"
            control.mkdir()
            settings = self._settings(Path(tmp) / "userdata")

            def fake_request(system=""):
                # Simulate the privileged worker confirming the repair.
                (control / "repair-rom-permissions.result").write_text("ok", encoding="utf-8")
                return True

            with mock.patch.dict("os.environ", {"DRONE_SERVICE_CONTROL_DIR": str(control)}), \
                 mock.patch("app.device.device_control._request_rom_permission_repair", side_effect=fake_request):
                self.assertTrue(drone_api._ensure_rom_write_access(settings, "snes", timeout_seconds=2))

    def test_ensure_rom_write_access_times_out_without_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "control"
            control.mkdir()
            settings = self._settings(Path(tmp) / "userdata")
            with mock.patch.dict("os.environ", {"DRONE_SERVICE_CONTROL_DIR": str(control)}), \
                 mock.patch("app.device.device_control._request_rom_permission_repair", return_value=True):
                self.assertFalse(drone_api._ensure_rom_write_access(settings, "snes", timeout_seconds=0.6))
            # And when the request itself can't be queued, it fails fast.
            with mock.patch("app.device.device_control._request_rom_permission_repair", return_value=False):
                self.assertFalse(drone_api._ensure_rom_write_access(settings, "snes", timeout_seconds=2))

    def test_artwork_gamelist_eacces_triggers_repair_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            peer = {"drone_id": "src", "reachable_url": "http://src:8080"}

            class FakeResponse:
                def __init__(self, data, name="images/Super Mario World-image.png"):
                    self._chunks = [data, b""]
                    self.headers = {"X-Asset-Relative-Path": name, "Content-Length": str(len(data))}

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self, _size=-1):
                    return self._chunks.pop(0)

                def geturl(self):
                    return "http://src:8080/v1/api/peer/artwork/snes/image/Super%20Mario%20World.zip"

            real_update = repo.update_gamelist_artwork_reference
            calls = {"n": 0}

            def flaky_update(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError("Operation not permitted")
                return real_update(*args, **kwargs)

            ensure = mock.Mock(return_value=True)
            with mock.patch("app.transfer.peer_download.urlopen", return_value=FakeResponse(b"ART")), \
                 mock.patch.object(repo, "update_gamelist_artwork_reference", side_effect=flaky_update), \
                 mock.patch("app.transfer.peer_download._ensure_rom_write_access", ensure):
                result = drone_api._download_artwork_from_peer(
                    settings, repo, {}, peer, "snes", "Super Mario World.zip", "image",
                    overwrite=True, local_rom_path="Super Mario World.zip",
                )

            # First PermissionError -> request a privileged perm repair, then retry.
            self.assertTrue(ensure.called)
            self.assertEqual(calls["n"], 2)
            self.assertEqual(result["gamelist_update_status"], "succeeded")
            gamelist = (root / "roms" / "snes" / "gamelist.xml").read_text(encoding="utf-8")
            self.assertIn("images/Super Mario World-image.png", gamelist)

    def test_artwork_video_lands_in_videos_subdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            peer = {"drone_id": "src", "reachable_url": "http://src:8080"}

            class FakeResponse:
                def __init__(self, data, name="videos/Peer-video.mp4"):
                    self._chunks = [data, b""]
                    self.headers = {"X-Asset-Relative-Path": name, "Content-Length": str(len(data))}

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def read(self, _size=-1):
                    return self._chunks.pop(0)

                def geturl(self):
                    return "http://src:8080/v1/api/peer/artwork/snes/video/Super%20Mario%20World.zip"

            with mock.patch("app.transfer.peer_download.urlopen", return_value=FakeResponse(b"VID")):
                result = drone_api._download_artwork_from_peer(
                    settings, repo, {}, peer, "snes", "Super Mario World.zip", "video",
                    overwrite=True, local_rom_path="Super Mario World.zip",
                )
            self.assertEqual(result["status"], "completed")
            # Video artwork must land under videos/ (named by the local ROM), not images/.
            target = root / "roms" / "snes" / "videos" / "Super Mario World-video.mp4"
            self.assertTrue(target.exists())
            gamelist = (root / "roms" / "snes" / "gamelist.xml").read_text(encoding="utf-8")
            self.assertIn("videos/Super Mario World-video.mp4", gamelist)

    def test_concurrent_gamelist_artwork_writes_do_not_clobber(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            roms = root / "roms" / "snes"
            names = [f"Game{i}.zip" for i in range(8)]
            for name in names:
                (roms / name).write_bytes(b"x")

            errors = []

            def worker(name, field):
                try:
                    repo.update_gamelist_artwork_reference("snes", name, field, f"images/{Path(name).stem}-{field}.png")
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(name, field))
                       for name in names for field in ("image", "marquee")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            # The serialized read-modify-write must preserve every reference; without
            # the lock the concurrent writers would drop most of them.
            self.assertEqual(errors, [])
            text = (roms / "gamelist.xml").read_text(encoding="utf-8")
            for name in names:
                stem = Path(name).stem
                self.assertIn(f"images/{stem}-image.png", text)
                self.assertIn(f"images/{stem}-marquee.png", text)

    def test_download_manager_pause_resume_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            self._seed_two_systems(root)
            settings = self._settings(root)
            repo = drone_api.RomRepository(root / "roms", root / "bios")
            with mock.patch("app.drone_api.Thread.start"):
                manager = drone_api.DownloadManager(settings, repo)

            manager.enqueue_rom({}, {"drone_id": "s"}, "snes", "A.zip")
            manager.enqueue_rom({}, {"drone_id": "s"}, "snes", "B.zip")

            paused = manager.pause()
            self.assertTrue(paused["paused"])
            self.assertTrue(manager.snapshot()["paused"])

            cleared = manager.clear_queue()
            self.assertEqual(cleared["cleared"], 2)
            self.assertEqual(len(manager.snapshot()["queued"]), 0)

            resumed = manager.resume()
            self.assertFalse(resumed["paused"])


class TailnetDiscoveryMergeTests(unittest.TestCase):
    def test_peer_pair_accepts_code_free_request_only_from_online_tailnet_ip(self) -> None:
        from app.web import handlers_peer

        handler = handlers_peer.HandlersPeerMixin()
        handler.settings = mock.MagicMock(device_id="local-b")
        handler.client_address = ("100.64.0.5", 12345)
        handler.server = mock.MagicMock()
        handler._send_json = mock.MagicMock()
        payload = {
            "tailnet_auto_pair": True,
            "drone_id": "local-a",
            "name": "Cabinet A",
            "certificate_pem": "-----BEGIN CERTIFICATE-----fake",
        }
        certificate = {"public_certificate": "-----BEGIN CERTIFICATE-----own", "fingerprint": "cc:dd"}
        manager = mock.MagicMock()
        manager.return_value.ensure_certificate.return_value = certificate
        with tempfile.TemporaryDirectory() as tmp:
            cert_path = Path(tmp) / "local-a.crt"
            cert_path.write_text("certificate", encoding="utf-8")
            with mock.patch.object(handlers_peer._local_network, "is_local_mode", return_value=True), \
                    mock.patch.object(handlers_peer, "tailnet_peer_ips", return_value={"100.64.0.5"}), \
                    mock.patch.object(handlers_peer._local_network, "validate_pairing_code") as validate, \
                    mock.patch.object(handlers_peer, "_save_local_peer_certificate", return_value=(cert_path, "aa:bb")), \
                    mock.patch.object(handlers_peer._local_network, "save_paired_peer", side_effect=lambda settings, peer: {**peer, "paired": True}) as saved, \
                    mock.patch.object(handlers_peer._local_network, "pairing_code"), \
                    mock.patch.object(handlers_peer, "DroneCertificateManager", manager), \
                    mock.patch.object(handlers_peer._local_network, "discovery_payload", return_value={"reachable_url": "https://cabinet-b.local"}):
                handler._handle_peer_pair(payload)
        validate.assert_not_called()
        self.assertEqual(saved.call_args[0][1]["pairing_source"], "tailnet")
        self.assertEqual(handler._send_json.call_args[0][0], 200)

    def test_nearby_excludes_paired_and_prefers_local_duplicate(self) -> None:
        from app.web import handlers_network

        handler = handlers_network.HandlersNetworkMixin()
        handler.settings = mock.MagicMock(use_fake_data=False)
        paired = {"drone_id": "paired-b", "name": "Paired B", "paired": True}
        local = {
            "drone_id": "local-c",
            "name": "Local C",
            "hostname": "cabinet-c",
            "tailnet_ip": "100.64.0.10",
            "source": "Local Network",
            "paired": False,
        }
        tailnet_devices = [
            {**local, "source": "Tailnet"},
            {"drone_id": "tailnet:phone", "name": "Phone", "hostname": "phone", "tailnet_ip": "100.64.0.20", "source": "Tailnet", "tailnet_device": True, "paired": False},
            {**paired, "source": "Tailnet", "tailnet_ip": "100.64.0.9"},
        ]
        manager = mock.MagicMock()
        manager.snapshot.return_value = {}
        with mock.patch.object(handlers_network._local_network, "paired_peers", return_value=[paired]), \
                mock.patch.object(handlers_network._local_network, "load_peer_checks", return_value=[]), \
                mock.patch.object(handlers_network._local_network, "discovered_peers", return_value=[paired, local]), \
                mock.patch.object(handlers_network._local_network, "is_local_mode", return_value=True), \
                mock.patch.object(handlers_network._local_network, "pairing_code", return_value={"code": "12345678"}), \
                mock.patch.object(handlers_network._local_network, "load_activity", return_value=[]), \
                mock.patch.object(handlers_network, "_network_mode", return_value="local_network"), \
                mock.patch.object(handlers_network, "_get_download_manager", return_value=manager):
            payload = handler._local_network_status_payload(tailnet_devices)
        self.assertEqual([peer["drone_id"] for peer in payload["peers"]], ["local-c", "tailnet:phone"])
        self.assertEqual(payload["peers"][0]["source"], "Local Network")
        self.assertEqual(payload["peers"][1]["source"], "Tailnet")

    def test_tailnet_sync_restores_route_on_existing_paired_peer(self) -> None:
        from app.web import handlers_network

        handler = handlers_network.HandlersNetworkMixin()
        handler.settings = mock.MagicMock(device_id="local-a")
        existing = {
            "drone_id": "local-b",
            "reachable_url": "https://192.168.1.22",
            "tailnet_ip": "",
            "certificate_fingerprint": "aa:bb",
            "paired": True,
        }
        device = {
            "tailnet_id": "node-b",
            "name": "Cabinet B",
            "hostname": "cabinet-b",
            "tailnet_ip": "100.64.0.9",
        }
        info = {
            "service": local_network.DISCOVERY_SERVICE,
            "drone_id": "local-b",
            "name": "Cabinet B",
            "hostname": "cabinet-b",
            "reachable_url": "https://cabinet-b.local",
            "certificate_fingerprint": "aa:bb",
        }
        with mock.patch.object(handlers_network._local_network, "discovered_peers", return_value=[]), \
                mock.patch.object(handlers_network._local_network, "record_discovered_peer", return_value=info), \
                mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=existing), \
                mock.patch.object(
                    handlers_network._local_network,
                    "save_paired_peer",
                    side_effect=lambda settings, peer: {**peer, "paired": True},
                ) as saved:
            result = handler._sync_tailnet_device(device, info=info)

        restored = saved.call_args.args[1]
        self.assertEqual(restored["reachable_url"], "https://192.168.1.22")
        self.assertEqual(restored["tailnet_ip"], "100.64.0.9")
        self.assertEqual(restored["tailnet_id"], "node-b")
        self.assertEqual(result["tailnet_ip"], "100.64.0.9")

    def test_tailnet_sync_does_not_rebind_peer_on_certificate_mismatch(self) -> None:
        from app.web import handlers_network

        handler = handlers_network.HandlersNetworkMixin()
        handler.settings = mock.MagicMock(device_id="local-a")
        existing = {
            "drone_id": "local-b",
            "tailnet_ip": "",
            "certificate_fingerprint": "aa:bb",
            "paired": True,
        }
        device = {"tailnet_id": "node-b", "tailnet_ip": "100.64.0.9"}
        info = {
            "service": local_network.DISCOVERY_SERVICE,
            "drone_id": "local-b",
            "certificate_fingerprint": "cc:dd",
        }
        with mock.patch.object(handlers_network._local_network, "discovered_peers", return_value=[]), \
                mock.patch.object(handlers_network._local_network, "record_discovered_peer", return_value=info), \
                mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=existing), \
                mock.patch.object(handlers_network._local_network, "save_paired_peer") as saved:
            result = handler._sync_tailnet_device(device, info=info)

        saved.assert_not_called()
        self.assertIn("fingerprint", result["tailnet_identity_error"])


class SwarmPageTests(unittest.TestCase):
    """The Swarm page: navbar entry, hash route, fleet cards, and automatic
    Tailnet discovery merged with Local Network discovery."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.js = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")
        cls.html = root.joinpath("app/web/templates/index.html").read_text(encoding="utf-8")

    def test_swarm_is_a_navbar_link_gated_like_other_admin_pages(self) -> None:
        self.assertIn('id="swarmMenuBtn" href="#admin/swarm"', self.html)
        self.assertIn('const swarmMenuBtn = document.getElementById("swarmMenuBtn");', self.js)
        self.assertIn('swarmMenuBtn.addEventListener("click"', self.js)
        visibility_start = self.js.index("function applyAdminVisibility()")
        visibility_end = self.js.index("function escapeHtml(", visibility_start)
        self.assertIn("swarmMenuBtn", self.js[visibility_start:visibility_end])

    def test_router_dispatches_swarm_hash(self) -> None:
        router_start = self.js.index("async function router()")
        router_body = self.js[router_start:self.js.index("catch (err)", router_start)]
        self.assertIn('hash === "#admin/swarm"', router_body)
        self.assertIn("await renderSwarmPage();", router_body)

    def test_swarm_page_loads_overview_and_renders_cards(self) -> None:
        page_start = self.js.index("async function renderSwarmPage()")
        page_end = self.js.index("async function renderIntegrationTransfersPanel", page_start)
        body = self.js[page_start:page_end]
        self.assertIn('api("/admin/swarm/overview")', body)
        self.assertIn("renderSwarmDroneCard", body)

    def test_swarm_page_fetches_discovery_and_overview_concurrently(self) -> None:
        # Regression: these two calls are independent (tailnet/discover is a
        # tailscale CLI subprocess + its own peer probing; swarm/overview is a
        # live probe of every paired peer) but were being awaited one after
        # the other, doubling the page's worst-case load time for no reason.
        page_start = self.js.index("async function renderSwarmPage()")
        page_end = self.js.index("async function renderIntegrationTransfersPanel", page_start)
        body = self.js[page_start:page_end]
        promise_all_index = body.index("Promise.all([")
        discover_index = body.index('apiPost("/admin/tailnet/discover"')
        overview_index = body.index('api("/admin/swarm/overview")')
        # Both calls must be arguments to the same Promise.all(...), i.e. both
        # indices fall between it and its closing "]);".
        promise_all_close = body.index("]);", promise_all_index)
        self.assertTrue(promise_all_index < discover_index < promise_all_close)
        self.assertTrue(promise_all_index < overview_index < promise_all_close)

        card_start = self.js.index("function renderSwarmDroneCard(")
        card_body = self.js[card_start:self.js.index("function swarmBrowsePeerAssets(", card_start)]
        for expected in (">This Drone<", ">Online<", ">Offline<", "Request Assets"):
            self.assertIn(expected, card_body)
        # Managing a peer directly (Manage button) replaced navigating to its
        # own separately-hosted UI in a new tab.
        self.assertNotIn("Open UI", card_body)
        self.assertNotIn("drone.ui_url", card_body)

    def test_manual_pair_by_address_ui_is_removed(self) -> None:
        page_start = self.js.index("async function renderSwarmPage()")
        page_body = self.js[page_start:self.js.index("async function renderIntegrationTransfersPanel", page_start)]
        self.assertNotIn("swarmPairByAddress", self.js)
        self.assertNotIn("Add Drone by Address", page_body)
        self.assertNotIn("swarmPairAddress", page_body)

    def test_request_assets_deep_links_into_the_transfers_picker(self) -> None:
        browse_start = self.js.index("function swarmBrowsePeerAssets(")
        browse_body = self.js[browse_start:self.js.index("function swarmManagePeer(", browse_start)]
        self.assertIn("localPeerAssetContext.peerId", browse_body)
        self.assertIn('setHash("#admin/transfers")', browse_body)

    def test_tailnet_card_explains_and_links_the_account_setup(self) -> None:
        card_start = self.js.index("function renderSwarmTailnetCard(")
        card_body = self.js[card_start:self.js.index("async function renderSwarmPage()", card_start)]
        # Why it's needed, where to sign up, where to fetch the key.
        self.assertIn("can't normally reach each other through home routers", card_body)
        self.assertIn('href="https://login.tailscale.com/start"', card_body)
        self.assertIn('href="https://login.tailscale.com/admin/settings/keys"', card_body)
        # The three states: connected / not installed / ready to enroll.
        self.assertIn(">Connected<", card_body)
        self.assertIn(">Not installed<", card_body)
        self.assertIn(">Not connected<", card_body)
        self.assertIn('id="swarmTailnetKey"', card_body)
        self.assertIn("swarmEnrollTailnet()", card_body)

    def test_tailnet_enroll_posts_the_key_to_the_enroll_endpoint(self) -> None:
        enroll_start = self.js.index("async function swarmEnrollTailnet()")
        enroll_body = self.js[enroll_start:self.js.index("function renderSwarmTailnetCard(", enroll_start)]
        self.assertIn('apiPost("/admin/tailnet/enroll", { auth_key: authKey })', enroll_body)

    def test_tailnet_connected_card_can_rotate_auth_token(self) -> None:
        rotate_start = self.js.index("async function swarmRotateTailnetAuthKey()")
        rotate_body = self.js[rotate_start:self.js.index("function renderSwarmTailnetCard(", rotate_start)]
        self.assertIn('apiPost("/admin/tailnet/rotate-auth-key", { auth_key: authKey })', rotate_body)
        self.assertIn("window.confirm", rotate_body)

        card_start = self.js.index("function renderSwarmTailnetCard(")
        card_body = self.js[card_start:self.js.index("async function renderSwarmPage()", card_start)]
        self.assertIn("Rotate Auth Token", card_body)
        self.assertIn('id="swarmTailnetRotateKey"', card_body)
        self.assertIn('type="password"', card_body)

    def test_admin_menu_has_no_redundant_swarm_tile(self) -> None:
        # Swarm is a navbar link (like Controls/Transfers); the Admin page
        # should not also carry a duplicate tile for it.
        menu_start = self.js.index("async function renderAdminMenu()")
        menu_end = self.js.index("async function updateDroneApp()")
        menu_body = self.js[menu_start:menu_end]
        self.assertNotIn("setHash('#admin/swarm')", menu_body)
        self.assertNotIn(">Swarm<", menu_body)

    def test_system_info_bar_shows_connected_count(self) -> None:
        bar_start = self.js.index("async function loadSystemInfoBar()")
        bar_end = self.js.index("async function router()", bar_start)
        body = self.js[bar_start:bar_end]
        self.assertIn('api("/admin/swarm/overview")', body)
        self.assertIn("Connected: ${connectedCount}", body)
        self.assertIn("filter(drone => drone.online)", body)

    def test_swarm_page_owns_pairing_and_nearby_drones(self) -> None:
        page_start = self.js.index("async function renderSwarmPage()")
        page_end = self.js.index("async function renderIntegrationTransfersPanel", page_start)
        body = self.js[page_start:page_end]
        self.assertIn('apiPost("/admin/tailnet/discover", {})', body)
        self.assertIn("renderSwarmTailnetCard(tailnet)", body)
        self.assertIn("Pairing code", body)
        self.assertIn("localPairCodeRotateBtn", body)
        self.assertIn("Nearby Drones", body)
        self.assertIn("renderLocalPeerRows(status.peers || [])", body)
        self.assertIn('class="col-12 col-lg-6"', body)
        self.assertNotIn("Enter this code on the other Drone", body)
        self.assertNotIn("Add Drone by Address", body)
        self.assertNotIn('onclick="renderSwarmPage()"><i class="bi bi-arrow-repeat', body)

    def test_nearby_table_has_source_and_paired_cards_have_forget(self) -> None:
        rows_start = self.js.index("function renderLocalPeerRows(")
        rows_body = self.js[rows_start:self.js.index("function localAssetPath(", rows_start)]
        self.assertIn("<th>Source</th>", rows_body)
        self.assertIn('peer.source === "Local Network"', rows_body)
        self.assertIn("Tailnet", rows_body)

        card_start = self.js.index("function renderSwarmDroneCard(")
        card_body = self.js[card_start:self.js.index("function swarmBrowsePeerAssets(", card_start)]
        self.assertIn("forgetLocalPeer", card_body)
        self.assertIn(">Forget</button>", card_body)


class RemoteAdminUiTests(unittest.TestCase):
    """Tab-scoped remote-drone impersonation: opening ?manage=<peer_id> in a
    new tab (from the Swarm page's Manage button) proxies every admin call in
    that tab to the peer, with no local/remote mixing to track per page."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.js = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")
        cls.html = root.joinpath("app/web/templates/index.html").read_text(encoding="utf-8")
        cls.css = root.joinpath("app/web/static/css/drone.css").read_text(encoding="utf-8")

    def test_banner_uses_a_high_contrast_theme_not_stock_bootstrap_warning(self) -> None:
        # Bootstrap's stock .alert-warning is pale yellow with dark text, but
        # this app's global ".fw-semibold { color: var(--admin-text) }" rule
        # (near-white) overrides that on the banner's bolded text, leaving
        # illegible white-on-pale-yellow -- the CSS must set an explicit,
        # high-contrast color for the banner text itself, not rely on
        # bootstrap's alert-warning defaults surviving untouched.
        self.assertIn("#managedPeerBanner.alert-warning", self.css)
        self.assertIn("#managedPeerBanner .managed-peer-banner-text", self.css)
        banner_rule = self.css[self.css.index("#managedPeerBanner.alert-warning"):]
        self.assertIn("background:", banner_rule[: banner_rule.index("}")])

    def test_connect_modal_is_dark_themed_like_the_rest_of_the_app(self) -> None:
        # Without this, .text-muted's pale text (styled for a dark background
        # everywhere else in this app) renders against bootstrap's stock white
        # modal instead -- illegible and visually inconsistent.
        self.assertIn("remote-connect-modal", self.css)
        self.assertIn('class="modal-content remote-connect-modal"', self.js)

    def test_manage_button_opens_a_new_tab_with_the_query_string(self) -> None:
        card_start = self.js.index("function renderSwarmDroneCard(")
        card_body = self.js[card_start:self.js.index("function swarmBrowsePeerAssets(", card_start)]
        self.assertIn("swarmManagePeer(decodeURIComponent(", card_body)
        self.assertIn(">Manage</button>", card_body)
        # Disabled for an offline peer -- nothing to connect to.
        self.assertIn('${drone.online ? "" : "disabled"}', card_body)

        fn_start = self.js.index("function swarmManagePeer(")
        fn_body = self.js[fn_start:self.js.index("\nfunction ", fn_start + 1)]
        self.assertIn('window.open(url.toString(), "_blank"', fn_body)
        self.assertIn("?manage=", fn_body)
        self.assertIn("manage_name=", fn_body)

    def test_banner_markup_is_hidden_by_default(self) -> None:
        self.assertIn('id="managedPeerBanner"', self.html)
        self.assertIn("d-none", self.html.split('id="managedPeerBanner"')[1].split(">")[0])
        self.assertIn('class="managed-peer-banner-text', self.html)
        self.assertIn("exitRemoteManagement()", self.html)

    def test_api_helpers_rewrite_to_the_generic_proxy_when_managing_a_peer(self) -> None:
        fn_start = self.js.index("function _apiRequestUrl(")
        fn_body = self.js[fn_start:self.js.index("\nasync function _handleApiUnauthorized", fn_start)]
        self.assertIn('managedPeer && !url.startsWith("/admin/remote/")', fn_body)
        self.assertIn("`${API_BASE}/remote/${encodeURIComponent(managedPeer.peerId)}${url}`", fn_body)
        # Absolute URLs and the connect/disconnect/status endpoints themselves
        # must never be rewritten -- they always mean this gateway.
        self.assertIn('url.startsWith("http://") || url.startsWith("https://")', fn_body)

    def test_unauthorized_handler_distinguishes_gateway_from_proxy_401s(self) -> None:
        fn_start = self.js.index("async function _handleApiUnauthorized(")
        fn_body = self.js[fn_start:self.js.index("\nasync function api(", fn_start)]
        self.assertIn('res.headers.get("WWW-Authenticate")', fn_body)
        self.assertIn("window.location.reload()", fn_body)
        # On a lost/rejected proxy session it reconnects then retries the
        # *same* call, rather than separately re-rendering and still
        # throwing -- which would otherwise race a fresh render against the
        # original caller's own (now stale) error handling.
        self.assertIn("showRemoteConnectGate(managedPeer)", fn_body)
        self.assertIn("return retry()", fn_body)

        api_start = self.js.index("async function api(url)")
        api_body = self.js[api_start:self.js.index("\nasync function apiPost(", api_start)]
        self.assertIn("_handleApiUnauthorized(res, () => api(url))", api_body)

        post_start = self.js.index("async function apiPost(url, payload)")
        post_body = self.js[post_start:self.js.index("\nasync function ", post_start + 1)]
        self.assertIn("_handleApiUnauthorized(res, () => apiPost(url, payload))", post_body)

    def test_bootstrap_checks_local_admin_before_impersonating(self) -> None:
        fn_start = self.js.index("async function bootstrap()")
        fn_body = self.js[fn_start:self.js.index("\nbootstrap();", fn_start)]
        probe_index = fn_body.index('api("/admin/configs/sources")')
        ready_index = fn_body.index("await ensureRemoteManagementReady();")
        info_bar_index = fn_body.index("loadSystemInfoBar();")
        router_index = fn_body.index("await router();")
        # Order matters: the local admin-enabled probe must run before
        # managedPeer can be set, and the status bar/router must run after,
        # so they proxy once a peer is being impersonated.
        self.assertTrue(probe_index < ready_index < info_bar_index < router_index)

    def test_ensure_remote_management_ready_checks_status_before_prompting(self) -> None:
        fn_start = self.js.index("async function ensureRemoteManagementReady()")
        fn_body = self.js[fn_start:self.js.index("\nfunction showRemoteConnectGate(", fn_start)]
        self.assertIn("_parseManageParam()", fn_body)
        self.assertIn("${API_BASE}/admin/remote/status?peer_id=", fn_body)
        self.assertIn("if (status.connected)", fn_body)
        self.assertIn("showRemoteConnectGate(requested)", fn_body)

    def test_connect_gate_posts_credentials_and_never_persists_them_client_side(self) -> None:
        fn_start = self.js.index("function showRemoteConnectGate(peer)")
        fn_body = self.js[fn_start:self.js.index("\nfunction ensureToastContainer()", fn_start)]
        self.assertIn("${API_BASE}/admin/remote/connect", fn_body)
        self.assertIn('body: JSON.stringify({ peer_id: peer.peerId, username, password })', fn_body)
        self.assertNotIn("sessionStorage", fn_body)
        self.assertNotIn("localStorage", fn_body)
        self.assertIn('data-bs-backdrop", "static"', fn_body)

    def test_exit_remote_management_disconnects_before_navigating_away(self) -> None:
        fn_start = self.js.index("async function exitRemoteManagement()")
        fn_body = self.js[fn_start:self.js.index("\nasync function ensureRemoteManagementReady", fn_start)]
        self.assertIn("${API_BASE}/admin/remote/disconnect", fn_body)
        self.assertIn("peer_id: managedPeer.peerId", fn_body)
        self.assertIn('url.search = ""', fn_body)

    def test_managed_peer_state_never_stores_a_credential(self) -> None:
        # The whole point: managedPeer only ever holds identity, never a secret.
        declaration_index = self.js.index("let managedPeer = null;")
        preceding_comment = self.js[max(0, declaration_index - 400):declaration_index]
        self.assertIn("never a credential", preceding_comment)


class TailnetServiceTests(unittest.TestCase):
    """UI-driven tailnet enrollment: status probing and `tailscale up` with a
    pasted auth key, never leaking the key into errors or logs."""

    def test_status_reports_not_installed_without_binaries(self) -> None:
        from app.device import tailnet_service

        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path("/nonexistent/tailscale")):
            status = tailnet_service.tailnet_status()
        self.assertFalse(status["installed"])
        self.assertFalse(status["running"])
        self.assertFalse(status["enrolled"])

    def test_status_parses_backend_state_and_tailnet_ip(self) -> None:
        from app.device import tailnet_service

        payload = {
            "BackendState": "Running",
            "Version": "1.80.0-tfake",
            "MagicDNSSuffix": "example.ts.net",
            "CurrentTailnet": {"Name": "example.ts.net", "MagicDNSSuffix": "example.ts.net"},
            "Health": ["relay connection is degraded"],
            "Self": {
                "TailscaleIPs": ["100.64.0.5", "fd7a:115c:a1e0::5"],
                "DNSName": "cabinet-a.example.ts.net.",
                "Relay": "dfw",
            },
            "Peer": {
                "nodekey:online": {
                    "ID": "peer-online",
                    "HostName": "cabinet-b",
                    "DNSName": "cabinet-b.example.ts.net.",
                    "TailscaleIPs": ["100.64.0.9", "fd7a:115c:a1e0::9"],
                    "Online": True,
                    "OS": "linux",
                },
                "nodekey:offline": {
                    "ID": "peer-offline",
                    "HostName": "cabinet-c",
                    "TailscaleIPs": ["100.64.0.10"],
                    "Online": False,
                },
            },
        }
        proc = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service.subprocess, "run", return_value=proc) as run:
            status = tailnet_service.tailnet_status()
        self.assertTrue(status["installed"])
        self.assertTrue(status["running"])
        self.assertTrue(status["enrolled"])
        self.assertEqual(status["tailnet_ip"], "100.64.0.5")
        self.assertEqual(status["version"], "1.80.0-tfake")
        self.assertEqual(status["dns_name"], "cabinet-a.example.ts.net")
        self.assertEqual(status["tailnet_name"], "example.ts.net")
        self.assertEqual(status["magic_dns_suffix"], "example.ts.net")
        self.assertEqual(status["relay"], "dfw")
        self.assertEqual(status["health"], ["relay connection is degraded"])
        self.assertEqual(len(status["peers"]), 1)
        self.assertEqual(status["peers"][0]["tailnet_id"], "peer-online")
        self.assertEqual(status["peers"][0]["tailnet_ip"], "100.64.0.9")
        self.assertIn("--json", run.call_args[0][0])

    def test_status_running_but_needs_login_is_not_enrolled(self) -> None:
        from app.device import tailnet_service

        proc = mock.Mock(returncode=0, stdout=json.dumps({"BackendState": "NeedsLogin"}), stderr="")
        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service.subprocess, "run", return_value=proc):
            status = tailnet_service.tailnet_status()
        self.assertTrue(status["running"])
        self.assertFalse(status["enrolled"])

    def test_enroll_requires_a_key_and_an_install(self) -> None:
        from app.device import tailnet_service

        with self.assertRaises(ValueError):
            tailnet_service.tailnet_enroll("   ")
        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path("/nonexistent/tailscale")):
            with self.assertRaisesRegex(RuntimeError, "Re-run the Drone installer"):
                tailnet_service.tailnet_enroll("tskey-auth-test")

    def test_enroll_runs_tailscale_up_and_never_echoes_the_key_on_failure(self) -> None:
        from app.device import tailnet_service

        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if "up" in command:
                return mock.Mock(returncode=1, stdout="", stderr="backend error: invalid key\n")
            return mock.Mock(returncode=0, stdout=json.dumps({"BackendState": "NeedsLogin"}), stderr="")

        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as caught:
                tailnet_service.tailnet_enroll("tskey-auth-SECRET")
        self.assertNotIn("SECRET", str(caught.exception))
        up_command = next(cmd for cmd in commands if "up" in cmd)
        self.assertIn("--accept-dns=false", up_command)
        self.assertIn("--netfilter-mode=off", up_command)
        self.assertTrue(any(arg.startswith("--authkey=") for arg in up_command))

    def test_startup_repairs_existing_tailnet_netfilter_preference(self) -> None:
        from app.device import tailnet_service

        proc = mock.Mock(returncode=0, stdout="", stderr="")
        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service, "_start_daemon_if_needed", return_value=None) as start_daemon, \
                mock.patch.object(tailnet_service.subprocess, "run", return_value=proc) as run:
            tailnet_service.ensure_tailnet_networking()

        start_daemon.assert_called_once_with()
        self.assertIn("set", run.call_args.args[0])
        self.assertIn("--netfilter-mode=off", run.call_args.args[0])

    def test_startup_does_not_configure_tailnet_when_daemon_cannot_start(self) -> None:
        from app.device import tailnet_service

        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service, "_start_daemon_if_needed", return_value="daemon unavailable"), \
                mock.patch.object(tailnet_service, "_run_cli") as run_cli:
            tailnet_service.ensure_tailnet_networking()

        run_cli.assert_not_called()

    def test_enroll_success_returns_fresh_status(self) -> None:
        from app.device import tailnet_service

        def fake_run(command, **kwargs):
            if "up" in command:
                return mock.Mock(returncode=0, stdout="", stderr="")
            payload = {"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.0.7"]}}
            return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service.subprocess, "run", side_effect=fake_run):
            status = tailnet_service.tailnet_enroll("tskey-auth-test")
        self.assertTrue(status["enrolled"])
        self.assertEqual(status["tailnet_ip"], "100.64.0.7")

    def test_rotate_auth_key_logs_out_then_reenrolls_without_returning_key(self) -> None:
        from app.device import tailnet_service

        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if "logout" in command or "up" in command:
                return mock.Mock(returncode=0, stdout="", stderr="")
            payload = {"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.0.8"]}}
            return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service.subprocess, "run", side_effect=fake_run):
            status = tailnet_service.tailnet_rotate_auth_key("tskey-auth-ROTATED")
        logout_index = next(index for index, command in enumerate(commands) if "logout" in command)
        up_index = next(index for index, command in enumerate(commands) if "up" in command)
        self.assertLess(logout_index, up_index)
        self.assertTrue(status["enrolled"])
        self.assertEqual(status["tailnet_ip"], "100.64.0.8")
        self.assertNotIn("ROTATED", json.dumps(status))

    def test_rotate_auth_key_requires_connected_tailnet(self) -> None:
        from app.device import tailnet_service

        proc = mock.Mock(returncode=0, stdout=json.dumps({"BackendState": "NeedsLogin"}), stderr="")
        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service.subprocess, "run", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "not connected"):
                tailnet_service.tailnet_rotate_auth_key("tskey-auth-test")

    def test_status_reports_own_tailscale_device_id(self) -> None:
        from app.device import tailnet_service

        payload = {"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.0.5"], "ID": "n123456CNTRL"}}
        proc = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service.subprocess, "run", return_value=proc):
            status = tailnet_service.tailnet_status()
        self.assertEqual(status["tailscale_device_id"], "n123456CNTRL")


class TailnetKeyExpiryTests(unittest.TestCase):
    """Opt-in, best-effort auto-disable of Tailscale key expiry so an
    unattended Drone never strands itself at NeedsLogin (see
    device/tailnet_service.py's module docstring)."""

    class _FakeHttpResponse:
        def __init__(self, payload: dict):
            self._body = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self._body

    def _fake_urlopen(self, token: str = "test-access-token", key_response: dict = None):
        calls = []

        def handler(request, timeout=None):
            calls.append(request)
            if request.full_url.endswith("/oauth/token"):
                return self._FakeHttpResponse({"access_token": token})
            return self._FakeHttpResponse(key_response if key_response is not None else {"keyExpiryDisabled": True})

        return calls, handler

    def test_disable_key_expiry_exchanges_oauth_token_and_updates_device(self) -> None:
        from app.device import tailnet_service

        calls, handler = self._fake_urlopen()
        status_payload = {"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.0.5"], "ID": "n123456CNTRL"}}
        proc = mock.Mock(returncode=0, stdout=json.dumps(status_payload), stderr="")
        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service.subprocess, "run", return_value=proc), \
                mock.patch.object(tailnet_service.urllib.request, "urlopen", side_effect=handler):
            result = tailnet_service.disable_key_expiry("client-id", "client-SECRET")

        self.assertEqual(result, {"device_id": "n123456CNTRL", "key_expiry_disabled": True})
        self.assertEqual(len(calls), 2)
        token_request, key_request = calls
        self.assertTrue(token_request.full_url.endswith("/oauth/token"))
        self.assertIn(b"client_id=client-id", token_request.data)
        self.assertIn(b"client_secret=client-SECRET", token_request.data)
        self.assertTrue(key_request.full_url.endswith("/device/n123456CNTRL/key"))
        self.assertEqual(key_request.get_header("Authorization"), "Bearer test-access-token")
        self.assertEqual(json.loads(key_request.data.decode("utf-8")), {"keyExpiryDisabled": True})

    def test_disable_key_expiry_requires_enrolled_device(self) -> None:
        from app.device import tailnet_service

        proc = mock.Mock(returncode=0, stdout=json.dumps({"BackendState": "NeedsLogin"}), stderr="")
        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service.subprocess, "run", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "Tailscale ID"):
                tailnet_service.disable_key_expiry("client-id", "client-secret")

    def test_disable_key_expiry_surfaces_oauth_failure(self) -> None:
        from app.device import tailnet_service

        def handler(request, timeout=None):
            return self._FakeHttpResponse({})  # no access_token

        status_payload = {"BackendState": "Running", "Self": {"ID": "n1"}}
        proc = mock.Mock(returncode=0, stdout=json.dumps(status_payload), stderr="")
        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service.subprocess, "run", return_value=proc), \
                mock.patch.object(tailnet_service.urllib.request, "urlopen", side_effect=handler):
            with self.assertRaisesRegex(RuntimeError, "access token"):
                tailnet_service.disable_key_expiry("client-id", "client-secret")

    def test_maybe_disable_key_expiry_noop_without_settings(self) -> None:
        from app.device import tailnet_service

        with mock.patch.object(tailnet_service, "disable_key_expiry") as disable:
            tailnet_service._maybe_disable_key_expiry(None)
        disable.assert_not_called()

    def test_maybe_disable_key_expiry_noop_without_oauth_credentials(self) -> None:
        from app.device import tailnet_service

        settings = mock.Mock(tailscale_oauth_client_id=None, tailscale_oauth_client_secret=None)
        with mock.patch.object(tailnet_service, "disable_key_expiry") as disable:
            tailnet_service._maybe_disable_key_expiry(settings)
        disable.assert_not_called()

    def test_maybe_disable_key_expiry_noop_when_not_enrolled(self) -> None:
        from app.device import tailnet_service

        settings = mock.Mock(tailscale_oauth_client_id="id", tailscale_oauth_client_secret="secret")
        with mock.patch.object(tailnet_service, "tailnet_status", return_value={"enrolled": False}), \
                mock.patch.object(tailnet_service, "disable_key_expiry") as disable:
            tailnet_service._maybe_disable_key_expiry(settings)
        disable.assert_not_called()

    def test_maybe_disable_key_expiry_calls_through_when_configured_and_enrolled(self) -> None:
        from app.device import tailnet_service

        settings = mock.Mock(tailscale_oauth_client_id="id", tailscale_oauth_client_secret="secret")
        with mock.patch.object(tailnet_service, "tailnet_status", return_value={"enrolled": True}), \
                mock.patch.object(tailnet_service, "disable_key_expiry") as disable:
            tailnet_service._maybe_disable_key_expiry(settings)
        disable.assert_called_once_with("id", "secret")

    def test_maybe_disable_key_expiry_swallows_failures(self) -> None:
        from app.device import tailnet_service

        settings = mock.Mock(tailscale_oauth_client_id="id", tailscale_oauth_client_secret="secret")
        with mock.patch.object(tailnet_service, "tailnet_status", return_value={"enrolled": True}), \
                mock.patch.object(tailnet_service, "disable_key_expiry", side_effect=RuntimeError("boom")):
            tailnet_service._maybe_disable_key_expiry(settings)  # must not raise

    def test_enroll_triggers_key_expiry_disable_when_configured(self) -> None:
        from app.device import tailnet_service

        def fake_run(command, **kwargs):
            if "up" in command:
                return mock.Mock(returncode=0, stdout="", stderr="")
            payload = {"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.0.7"], "ID": "n7"}}
            return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        settings = mock.Mock(tailscale_oauth_client_id="id", tailscale_oauth_client_secret="secret")
        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(tailnet_service, "disable_key_expiry") as disable:
            tailnet_service.tailnet_enroll("tskey-auth-test", settings)
        disable.assert_called_once_with("id", "secret")

    def test_enroll_without_settings_does_not_attempt_key_expiry_disable(self) -> None:
        from app.device import tailnet_service

        def fake_run(command, **kwargs):
            if "up" in command:
                return mock.Mock(returncode=0, stdout="", stderr="")
            payload = {"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.0.7"], "ID": "n7"}}
            return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(tailnet_service, "disable_key_expiry") as disable:
            tailnet_service.tailnet_enroll("tskey-auth-test")
        disable.assert_not_called()

    def test_ensure_tailnet_networking_attempts_key_expiry_disable(self) -> None:
        from app.device import tailnet_service

        proc = mock.Mock(returncode=0, stdout="", stderr="")
        settings = mock.Mock(tailscale_oauth_client_id="id", tailscale_oauth_client_secret="secret")
        with tempfile.NamedTemporaryFile() as fake_cli, \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", Path(fake_cli.name)), \
                mock.patch.object(tailnet_service, "_start_daemon_if_needed", return_value=None), \
                mock.patch.object(tailnet_service.subprocess, "run", return_value=proc), \
                mock.patch.object(tailnet_service, "tailnet_status", return_value={"enrolled": True}), \
                mock.patch.object(tailnet_service, "disable_key_expiry") as disable:
            tailnet_service.ensure_tailnet_networking(settings)
        disable.assert_called_once_with("id", "secret")


class InstallerTailscaleTests(unittest.TestCase):
    """batocera_install.sh sets up the Tailscale mesh (binaries under /userdata,
    DRONE_TAILNET service, optional auth-key enrollment) so the tailnet needs no
    manual install; batocera_uninstall.sh removes it symmetrically."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.install = root.joinpath("scripts/batocera_install.sh").read_text(encoding="utf-8")
        cls.uninstall = root.joinpath("scripts/batocera_uninstall.sh").read_text(encoding="utf-8")
        cls.install_path = str(root / "scripts/batocera_install.sh")
        cls.uninstall_path = str(root / "scripts/batocera_uninstall.sh")

    def test_scripts_parse_cleanly(self) -> None:
        for path in (self.install_path, self.uninstall_path):
            result = subprocess.run(["sh", "-n", path], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_places_everything_under_userdata(self) -> None:
        # Binaries + state must survive Batocera OS updates (read-only rootfs).
        self.assertIn('TS_DIR="/userdata/system/tailscale"', self.install)
        self.assertIn('TS_SERVICE="/userdata/system/services/DRONE_TAILNET"', self.install)
        self.assertIn('--statedir="$STATE_DIR"', self.install)

    def test_installer_supports_hands_free_and_skip_paths(self) -> None:
        self.assertIn("DRONE_SKIP_TAILSCALE", self.install)
        self.assertIn("TS_AUTHKEY", self.install)
        self.assertIn("--authkey=", self.install)
        self.assertIn("--netfilter-mode=off", self.install)
        # Keep Batocera's DNS untouched; the integration works on raw 100.x IPs.
        self.assertIn("--accept-dns=false", self.install)
        # A mesh-setup failure must not fail the Drone install itself.
        self.assertIn("if ! install_tailscale_mesh; then", self.install)

    def test_service_falls_back_to_userspace_networking_without_tun(self) -> None:
        self.assertIn("modprobe tun", self.install)
        self.assertIn("--tun=userspace-networking", self.install)

    def test_uninstaller_removes_the_mesh_and_releases_the_node(self) -> None:
        self.assertIn("remove_tailscale_mesh", self.uninstall)
        self.assertIn("logout", self.uninstall)
        self.assertIn('rm -rf "$TS_DIR"', self.uninstall)
        self.assertIn("DRONE_KEEP_TAILSCALE", self.uninstall)


class NavRestructureTests(unittest.TestCase):
    """Theme moved from a navbar link to an Admin-page card; Integration's
    Transfers tab became its own navbar-linked page, leaving Integration as a
    Configuration-only page."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.js = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")
        cls.html = root.joinpath("app/web/templates/index.html").read_text(encoding="utf-8")

    def test_theme_is_not_a_navbar_link(self) -> None:
        self.assertNotIn('id="themeMenuBtn"', self.html)
        self.assertNotIn('const themeMenuBtn = document.getElementById("themeMenuBtn");', self.js)
        self.assertNotIn("themeMenuBtn.addEventListener", self.js)

    def test_theme_is_an_admin_menu_card(self) -> None:
        menu_start = self.js.index("async function renderAdminMenu()")
        menu_end = self.js.index("async function updateDroneApp()")
        body = self.js[menu_start:menu_end]
        self.assertIn("setHash('#theme')", body)
        self.assertIn(">Theme<", body)

    def test_theme_page_route_is_unchanged(self) -> None:
        # Moving the entry point doesn't gate or rename the page itself.
        self.assertIn('hash === "#theme"', self.js)
        self.assertIn("await renderThemeGalleryPage();", self.js)

    def test_transfers_is_an_admin_gated_navbar_link(self) -> None:
        self.assertIn('id="transfersMenuBtn" href="#admin/transfers"', self.html)
        controls_pos = self.html.index('id="controlsMenuBtn"')
        transfers_pos = self.html.index('id="transfersMenuBtn"')
        admin_pos = self.html.index('id="adminMenuBtn"')
        self.assertTrue(controls_pos < transfers_pos < admin_pos)

        self.assertIn('const transfersMenuBtn = document.getElementById("transfersMenuBtn");', self.js)
        self.assertIn(
            "const adminLinks = [adminMenuBtn, controlsMenuBtn, transfersMenuBtn, swarmMenuBtn, apiAccessBtn]",
            self.js,
        )
        click_start = self.js.index('transfersMenuBtn.addEventListener("click"')
        click_end = self.js.index("});", click_start)
        click_body = self.js[click_start:click_end]
        self.assertIn("if (!adminEnabled) return;", click_body)
        self.assertIn('setHash("#admin/transfers");', click_body)

    def test_router_dispatches_transfers_and_redirects_integration_to_swarm(self) -> None:
        transfers_route = self.js.index('} else if (hash === "#admin/transfers")')
        self.assertIn("await renderTransfersPage();", self.js[transfers_route:transfers_route + 200])

        integration_route = self.js.index('hash.startsWith("#admin/integration")')
        self.assertIn('setHash("#admin/swarm");', self.js[integration_route:integration_route + 500])

    def test_transfers_page_renders_transfers_panel_and_auto_refreshes(self) -> None:
        page_start = self.js.index("async function renderTransfersPage()")
        page_end = self.js.index("async function renderIntegrationTransfersPanel(")
        body = self.js[page_start:page_end]
        self.assertIn('titleNode.textContent = "Transfers";', body)
        self.assertIn("integrationTransfersPanel", body)
        self.assertIn("renderIntegrationTransfersPanel(document.getElementById(\"integrationTransfersPanel\"))", body)
        self.assertIn("startTransfersAutoRefresh();", body)

        refresh_start = self.js.index("function startTransfersAutoRefresh()")
        refresh_end = self.js.index("function startLogAutoRefresh()", refresh_start)
        refresh_body = self.js[refresh_start:refresh_end]
        self.assertIn('Promise.all([api("/admin/downloads"), api("/admin/uploads")])', refresh_body)
        self.assertIn("renderTransfersPanel(downloads, uploads)", refresh_body)

    def test_transfer_request_uses_independent_file_options_and_small_buttons(self) -> None:
        panel_start = self.js.index("async function renderLocalTransferRequestPanel(")
        panel_end = self.js.index("\nfunction localAssetIncludeArtwork()")
        body = self.js[panel_start:panel_end]
        self.assertIn('id="localAssetIncludeArtwork" checked', body)
        self.assertIn('for="localAssetIncludeArtwork">Include Artwork</label>', body)
        self.assertIn('id="localAssetIncludeRoms" checked', body)
        self.assertIn('for="localAssetIncludeRoms">Include ROMs</label>', body)
        self.assertIn('for="localAssetOverwriteFiles">Overwrite Files</label>', body)
        self.assertNotIn("localAssetArtworkOnly", body)
        self.assertIn('class="btn btn-sm btn-primary" id="localAssetLoadBtn"', body)
        self.assertIn('class="btn btn-sm btn-success" id="localAssetCopyAllBtn"', body)

    def test_legacy_hash_redirects_point_at_new_pages(self) -> None:
        downloads_start = self.js.index('hash === "#admin/downloads"')
        downloads_end = self.js.index("} else if", downloads_start)
        self.assertIn('setHash("#admin/transfers");', self.js[downloads_start:downloads_end])

        # Overmind/local-network/integration hashes all land on the Swarm page.
        overmind_start = self.js.index('["#admin/overmind", "#admin/overmind/actions", "#admin/local-network"]')
        overmind_end = self.js.index("} else if", overmind_start)
        self.assertIn('setHash("#admin/swarm");', self.js[overmind_start:overmind_end])

    def test_peer_browse_buttons_deep_link_via_swarm_helper(self) -> None:
        rows_start = self.js.index("function renderLocalPeerRows(")
        rows_end = self.js.index("\nasync function ", rows_start + 1)
        body = self.js[rows_start:rows_end]
        self.assertIn("swarmBrowsePeerAssets(", body)
        self.assertNotIn("browseLocalPeer(", body)

    def test_transfer_request_panel_never_auto_requests_assets(self) -> None:
        # Asset data loads only on an explicit Request click. A page visit (or
        # a Swarm-card deep link) may preselect a drone and load its SYSTEMS,
        # but never its assets.
        self.assertNotIn("pendingLocalPeerBrowse", self.js)
        self.assertNotIn("autoLoadedPeerId", self.js)

        fn_start = self.js.index("async function renderLocalTransferRequestPanel(target)")
        fn_end = self.js.index("\nfunction localAssetIncludeArtwork()")
        body = self.js[fn_start:fn_end]
        self.assertIn('&lt;Select Drone&gt;', body)
        self.assertIn('api("/admin/swarm/overview")', body)
        self.assertIn("!drone.is_self && drone.online", body)
        self.assertIn("await loadLocalPeerSystems();", body)
        self.assertNotIn("loadLocalPeerAssets()", body)
        self.assertIn("await onPeerSelected();", body)

    def test_downloads_page_and_refresh_view_point_at_transfers(self) -> None:
        self.assertIn('onclick="setHash(\'#admin/transfers\')">Back to Transfers</button>', self.js)
        refresh_start = self.js.index("async function refreshDownloadsView()")
        refresh_end = self.js.index("\nasync function ", refresh_start + 1)
        self.assertIn('window.location.hash === "#admin/transfers"', self.js[refresh_start:refresh_end])


class RomMediaVideoAndMetadataTests(unittest.TestCase):
    """The ROM details page (#system/{system}/rom/{id}) grew a full gamelist
    metadata CRUD form and a video player wired to the gamelist <video>
    reference, shared with the Artwork admin page's upload widgets."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.js = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")

    def test_render_rom_media_page_includes_video_player_and_metadata_form(self) -> None:
        fn_start = self.js.index("async function renderRomMediaPage(")
        fn_end = self.js.index("\nfunction renderThemeGallery(")
        body = self.js[fn_start:fn_end]
        self.assertIn('id="romMediaVideoPlayer"', body)
        self.assertIn("romVideoUrl(rom)", body)
        self.assertIn("romMetadataEditFormHtml(rom)", body)
        self.assertIn('document.getElementById("romMediaVideoBody")', body)

    def test_video_upload_row_accepts_video_files_and_resolves_a_video_url(self) -> None:
        fn_start = self.js.index("function artworkImageUploadHtml(")
        fn_end = self.js.index("\nfunction artworkExistingImageUrl(")
        body = self.js[fn_start:fn_end]
        self.assertIn('isVideo ? "video/*" : "image/*"', body)
        self.assertIn("artworkExistingAssetUrl(rom, field, existingValue)", body)
        self.assertIn('data-is-video="${isVideo ? "1" : "0"}"', body)

    def test_existing_video_url_uses_the_public_video_route_not_the_image_route(self) -> None:
        fn_start = self.js.index("function artworkExistingVideoUrl(")
        fn_end = self.js.index("\nfunction artworkExistingAssetUrl(")
        body = self.js[fn_start:fn_end]
        self.assertIn("/public/systems/", body)
        self.assertIn("/video/", body)

    def test_view_button_opens_video_lightbox_for_video_field(self) -> None:
        fn_start = self.js.index("function bindArtworkEditButtons(")
        fn_end = self.js.index("\nasync function uploadArtworkImage(")
        body = self.js[fn_start:fn_end]
        self.assertIn('data-is-video") === "1"', body)
        self.assertIn("showVideoLightbox(url, title)", body)

    def test_video_lightbox_stops_playback_on_close(self) -> None:
        fn_start = self.js.index("function showVideoLightbox(")
        fn_end = self.js.index("\nfunction appendCacheBust(")
        body = self.js[fn_start:fn_end]
        self.assertIn("video.pause();", body)

    def test_upload_refresh_is_page_aware_instead_of_assuming_artwork_admin_page(self) -> None:
        # uploadArtworkImage is shared by the Artwork admin page and the ROM
        # Details page; unconditionally calling the admin page's own
        # selectArtworkRom() used to crash with a null #selectedArtworkRom
        # lookup whenever an upload happened from the ROM Details page.
        fn_start = self.js.index("async function uploadArtworkImage(")
        fn_end = self.js.index("\nfunction openMarqueeCropper(")
        body = self.js[fn_start:fn_end]
        self.assertIn('document.getElementById("selectedArtworkRom")', body)
        self.assertIn("renderRomMediaPage(rom.system, rom.unique_id)", body)

    def test_save_rom_media_metadata_posts_gamelist_update(self) -> None:
        fn_start = self.js.index("async function saveRomMediaMetadata(")
        fn_end = self.js.index("\nasync function removeRomMediaGamelistEntry(")
        body = self.js[fn_start:fn_end]
        self.assertIn('apiPost("/admin/artwork/gamelist/update"', body)

    def test_remove_rom_media_gamelist_entry_confirms_before_posting(self) -> None:
        fn_start = self.js.index("async function removeRomMediaGamelistEntry(")
        fn_end = self.js.index("\nfunction artworkPayloadUrl(")
        body = self.js[fn_start:fn_end]
        self.assertIn("window.confirm(", body)
        self.assertIn('apiPost("/admin/artwork/gamelist/remove"', body)

    def test_metadata_form_excludes_artwork_and_video_fields(self) -> None:
        fn_start = self.js.index("function romMetadataEditFormHtml(")
        fn_end = self.js.index("\nasync function saveRomMediaMetadata(")
        body = self.js[fn_start:fn_end]
        self.assertIn("ROM_MEDIA_ARTWORK_FIELDS", body)


class GuessContentTypeVideoTests(unittest.TestCase):
    def test_guess_content_type_recognizes_common_video_containers(self) -> None:
        handler = object.__new__(drone_api.RomRequestHandler)
        self.assertEqual(handler._guess_content_type(Path("clip.mp4")), "video/mp4")
        self.assertEqual(handler._guess_content_type(Path("clip.webm")), "video/webm")
        self.assertEqual(handler._guess_content_type(Path("clip.mkv")), "video/x-matroska")
        self.assertEqual(handler._guess_content_type(Path("clip.mov")), "video/quicktime")
        self.assertEqual(handler._guess_content_type(Path("clip.avi")), "video/x-msvideo")
