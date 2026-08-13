"""DroneApiClient driven against the real Drone server (not a fake).

Uses the same create_server()-on-a-daemon-thread harness as
tests/test_integration_mock_server.py in the main app, so this proves
ports-client's stdlib HTTP client is actually compatible with the live
Drone API contract, not just a hand-written mimic of it.
"""

import os
import tempfile
import threading
import unittest
from pathlib import Path

from app.drone_api import Settings, create_server
from app.mock_data import seed_mock_userdata
from client.config import ClientConfig
from client.endpoints import (
    config_backups,
    create_config_backup,
    local_network_discover,
    local_network_pair,
    local_network_rotate_pairing_code,
    local_network_status,
    network_share_enable,
    network_shares,
    peer_asset_summary,
    peer_movies,
    peer_roms,
    request_asset,
    swarm_overview,
    tailnet_enroll,
    tailnet_status,
    vpn_connect,
    vpn_status,
)
from client.errors import AuthenticationError, DroneApiError
from client.http_client import DroneApiClient

USERNAME = "admin"
PASSWORD = "changeme"


class DroneApiClientIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name) / "userdata"
        seed_mock_userdata(root)

        self._old_env = dict(os.environ)
        os.environ["USERDATA_ROOT"] = str(root)
        os.environ["ROMS_ROOT"] = str(root / "roms")
        os.environ["BIOS_ROOT"] = str(root / "bios")
        os.environ["SAVES_ROOT"] = str(root / "saves")
        os.environ["THEMES_ROOT"] = str(root / "themes")
        os.environ["MOVIES_ROOT"] = str(root / "movies")
        os.environ["MUSIC_ROOT"] = str(root / "music")
        os.environ["BATOCERA_CONF_FILE"] = str(root / "system" / "batocera.conf")
        os.environ["ES_SETTINGS_FILE"] = str(root / "system" / "configs" / "emulationstation" / "es_settings.cfg")
        os.environ["DRONE_APP_USERNAME"] = USERNAME
        os.environ["DRONE_APP_PASSWORD"] = PASSWORD
        os.environ["HTTPS_PORT"] = "0"
        os.environ["HTTP_ONLY"] = "1"
        os.environ["DRONE_LOCAL_ALLOW_INSECURE_HTTP"] = "1"
        os.environ["LOG_DIR"] = str(Path(self._tmp.name) / "logs")
        os.environ["USE_FAKE_DATA"] = "1"
        os.environ["ROM_METADATA_POLL_SECONDS"] = "0"

        self.settings = Settings.from_env()
        try:
            self.server = create_server(self.settings)
        except PermissionError as error:
            self.skipTest(f"Socket bind is not allowed in this environment: {error}")
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self.session_file = Path(self._tmp.name) / "ports-client-session.json"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        os.environ.clear()
        os.environ.update(self._old_env)
        self._tmp.cleanup()

    def _client(self) -> DroneApiClient:
        config = ClientConfig(
            host="127.0.0.1",
            https_port=self.port,
            http_only=True,
            ca_cert_file=Path("/unused"),
            session_cookie_path=self.session_file,
        )
        return DroneApiClient(config)

    def test_login_then_session_status_round_trip(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)
        self.assertTrue(client.session_status()["authenticated"])

    def test_wrong_password_is_rejected_by_the_real_server(self) -> None:
        client = self._client()
        with self.assertRaises(AuthenticationError):
            client.login(USERNAME, "not-the-password")

    def test_relaunch_reuses_persisted_session_against_real_server(self) -> None:
        first = self._client()
        first.login(USERNAME, PASSWORD)

        second = self._client()  # simulates the app being relaunched from Ports
        result = swarm_overview(second)
        self.assertIn("active", result)

    def test_swarm_overview_round_trip(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        result = swarm_overview(client)
        self.assertIn("active", result)
        self.assertIn("drones", result)
        self_entry = next(row for row in result["drones"] if row.get("is_self"))
        self.assertTrue(self_entry["online"])

    def test_vpn_status_round_trip_with_no_config(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        status = vpn_status(client)
        self.assertIn("status", status)
        self.assertFalse(status["has_config"])

    def test_vpn_connect_with_no_config_raises_with_real_error_message(self) -> None:
        # Also exercises the real server's actual {"errors": [...]} shape for
        # a failed connect -- not just the hand-written fake in
        # test_http_client.py.
        client = self._client()
        client.login(USERNAME, PASSWORD)

        with self.assertRaises(DroneApiError) as ctx:
            vpn_connect(client)
        self.assertNotIn("HTTP 400", str(ctx.exception))

    def test_config_backups_list_and_create_round_trip(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        empty = config_backups(client)
        self.assertEqual(empty.get("backups"), [])

        created = create_config_backup(client)
        self.assertEqual(created.get("status"), "ok")
        self.assertIn("backup", created)

        listed = config_backups(client)
        self.assertEqual(len(listed.get("backups", [])), 1)

    def test_tailnet_status_round_trip(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        status = tailnet_status(client)
        self.assertIn("installed", status)
        self.assertIn("enrolled", status)

    def test_tailnet_enroll_without_key_raises_with_real_error_message(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        with self.assertRaises(DroneApiError) as ctx:
            tailnet_enroll(client, "")
        self.assertIn("auth key is required", str(ctx.exception))

    def test_local_network_status_round_trip(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        status = local_network_status(client)
        self.assertIn("pairing", status)
        self.assertEqual(len(status["pairing"]["code"]), 8)
        self.assertEqual(status.get("peers"), [])

    def test_local_network_discover_round_trip(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        result = local_network_discover(client)
        self.assertIn("pairing", result)
        self.assertIn("announcement_sent", result)

    def test_local_network_rotate_pairing_code_changes_the_code(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        original = local_network_status(client)["pairing"]["code"]
        rotated = local_network_rotate_pairing_code(client)["pairing"]["code"]
        self.assertNotEqual(original, rotated)

    def test_local_network_pair_with_unknown_peer_is_a_clean_404(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        with self.assertRaises(DroneApiError) as ctx:
            local_network_pair(client, "not-a-real-peer-id", "00000000")
        self.assertIn("discovered peer not found", str(ctx.exception))

    def test_network_shares_list_starts_empty(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        self.assertEqual(network_shares(client)["shares"], [])

    def test_network_share_enable_for_unpaired_peer_is_a_clean_error(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        with self.assertRaises(DroneApiError) as ctx:
            network_share_enable(client, "not-a-real-peer-id")
        self.assertIn("not a paired peer", str(ctx.exception))

    def test_peer_asset_summary_for_unpaired_peer_is_a_clean_404(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        with self.assertRaises(DroneApiError) as ctx:
            peer_asset_summary(client, "not-a-real-peer-id")
        self.assertIn("paired peer not found", str(ctx.exception))

    def test_peer_roms_for_unpaired_peer_is_a_clean_404(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        with self.assertRaises(DroneApiError) as ctx:
            peer_roms(client, "not-a-real-peer-id", "snes")
        self.assertIn("paired peer not found", str(ctx.exception))

    def test_peer_movies_for_unpaired_peer_is_a_clean_404(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        with self.assertRaises(DroneApiError) as ctx:
            peer_movies(client, "not-a-real-peer-id")
        self.assertIn("paired peer not found", str(ctx.exception))

    def test_request_asset_for_unpaired_peer_is_a_clean_404(self) -> None:
        client = self._client()
        client.login(USERNAME, PASSWORD)

        with self.assertRaises(DroneApiError) as ctx:
            request_asset(client, "not-a-real-peer-id", "roms", {"name": "Chrono Trigger"}, system="snes")
        self.assertIn("paired peer not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
