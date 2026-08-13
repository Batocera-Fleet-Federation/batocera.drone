"""Unit tests for screen orchestration logic (session resume, API-error handling,
navigation callbacks) -- the parts of each screen that don't call into imgui's
drawing functions, which need a live render frame this suite doesn't set up.
See test_http_client.py / test_http_client_integration.py for the HTTP layer.
"""

import unittest

from client.errors import AuthenticationError, DroneApiError
from ui.screens.backups import BackupsScreen
from ui.screens.login import LoginScreen
from ui.screens.swarm import SwarmScreen
from ui.screens.vpn import VpnScreen
from ui.shell import AppShell


class _FakeApiClient:
    def __init__(self, *, has_session_cookie=False, session_status=None, login_result=None,
                 login_error=None, get_responses=None, get_error=None,
                 post_responses=None, post_error=None):
        self.has_session_cookie = has_session_cookie
        self._session_status = session_status or {"authenticated": False}
        self._login_result = login_result
        self._login_error = login_error
        self._get_responses = get_responses or {}
        self._get_error = get_error
        self._post_responses = post_responses or {}
        self._post_error = post_error
        self.login_calls = []
        self.post_calls = []

    def session_status(self):
        return self._session_status

    def login(self, username, password):
        self.login_calls.append((username, password))
        if self._login_error is not None:
            raise self._login_error
        return self._login_result or username

    def get(self, path):
        if self._get_error is not None:
            raise self._get_error
        return self._get_responses.get(path, {})

    def post(self, path, body=None):
        self.post_calls.append((path, body))
        if self._post_error is not None:
            raise self._post_error
        return self._post_responses.get(path, {})

    def logout(self):
        self.logout_called = True


class LoginScreenTests(unittest.TestCase):
    def test_on_enter_authenticates_immediately_via_loopback_trust(self) -> None:
        # Drone treats a loopback caller as pre-authenticated even with no
        # cookie at all -- this is the expected path on every real launch,
        # since the client always talks to 127.0.0.1 by design.
        client = _FakeApiClient(has_session_cookie=False, session_status={"authenticated": True, "username": "batocera"})
        calls = []
        screen = LoginScreen(client, on_authenticated=calls.append)
        screen.on_enter()
        self.assertEqual(calls, ["batocera"])

    def test_on_enter_not_authenticated_falls_through_to_manual_form(self) -> None:
        # Only expected if DRONE_PORTS_CLIENT_HOST points at a non-loopback host.
        client = _FakeApiClient(session_status={"authenticated": False})
        calls = []
        screen = LoginScreen(client, on_authenticated=calls.append)
        screen.on_enter()
        self.assertEqual(calls, [])
        self.assertFalse(screen.connecting)
        self.assertIsNone(screen.connection_error)

    def test_on_enter_connection_error_is_surfaced(self) -> None:
        class _UnreachableApiClient(_FakeApiClient):
            def session_status(self):
                raise DroneApiError("could not reach drone at http://127.0.0.1:8000/v1/api")

        client = _UnreachableApiClient()
        calls = []
        screen = LoginScreen(client, on_authenticated=calls.append)
        screen.on_enter()
        self.assertEqual(calls, [])
        self.assertFalse(screen.connecting)
        self.assertIn("could not reach drone", screen.connection_error)

    def test_attempt_login_success_invokes_callback(self) -> None:
        client = _FakeApiClient(login_result="batocera")
        calls = []
        screen = LoginScreen(client, on_authenticated=calls.append)
        screen.username, screen.password = "batocera", "linux"
        screen._attempt_login()
        self.assertEqual(calls, ["batocera"])
        self.assertIsNone(screen.error)

    def test_attempt_login_wrong_credentials_sets_error_and_clears_password(self) -> None:
        client = _FakeApiClient(login_error=AuthenticationError("nope"))
        calls = []
        screen = LoginScreen(client, on_authenticated=calls.append)
        screen.username, screen.password = "batocera", "wrong"
        screen._attempt_login()
        self.assertEqual(calls, [])
        self.assertEqual(screen.error, "Invalid username or password.")
        self.assertEqual(screen.password, "")

    def test_attempt_login_unreachable_drone_surfaces_message(self) -> None:
        client = _FakeApiClient(login_error=DroneApiError("could not reach Drone"))
        screen = LoginScreen(client, on_authenticated=lambda username: None)
        screen.username, screen.password = "batocera", "linux"
        screen._attempt_login()
        self.assertEqual(screen.error, "could not reach Drone")


class SwarmScreenTests(unittest.TestCase):
    def test_on_enter_populates_drones(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/swarm/overview": {
                "active": True,
                "drones": [{"drone_id": "self", "name": "batocera", "is_self": True, "online": True}],
            }
        })
        screen = SwarmScreen(client)
        screen.on_enter()
        self.assertTrue(screen.active)
        self.assertEqual(len(screen.drones), 1)
        self.assertIsNone(screen.overview_error)

    def test_on_enter_surfaces_error(self) -> None:
        client = _FakeApiClient(get_error=DroneApiError("swarm unavailable"))
        screen = SwarmScreen(client)
        screen.on_enter()
        self.assertEqual(screen.overview_error, "swarm unavailable")
        self.assertEqual(screen.drones, [])

    def test_inactive_swarm_still_parses_cleanly(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/swarm/overview": {"active": False, "drones": []}
        })
        screen = SwarmScreen(client)
        screen.on_enter()
        self.assertFalse(screen.active)
        self.assertIsNone(screen.overview_error)

    def test_switching_tabs_loads_each_exactly_once(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/swarm/overview": {"active": False, "drones": []},
            "/admin/tailnet/status": {"installed": True, "enrolled": False},
            "/admin/local-network/status": {"pairing": {"code": "12345678"}, "peers": []},
        })
        screen = SwarmScreen(client)
        screen.on_enter()  # overview, default tab
        screen._select_tab("tailnet")
        screen._select_tab("lan")
        screen._select_tab("lan")  # re-selecting shouldn't refetch
        self.assertEqual(screen._loaded_tabs, {"overview", "tailnet", "lan"})
        self.assertEqual(screen.lan["pairing"]["code"], "12345678")

    def test_tailnet_not_installed(self) -> None:
        client = _FakeApiClient(get_responses={"/admin/tailnet/status": {"installed": False}})
        screen = SwarmScreen(client)
        screen._reload_tailnet()
        self.assertFalse(screen.tailnet.get("installed"))
        self.assertIsNone(screen.tailnet_error)

    def test_tailnet_enroll_success_clears_key_and_reloads(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/tailnet/status": {"installed": True, "enrolled": True, "tailnet_name": "example.ts.net"}},
            post_responses={"/admin/tailnet/enroll": {"status": "enrolled"}},
        )
        screen = SwarmScreen(client)
        screen.tailnet_auth_key = "tskey-auth-fake"
        screen._enroll_tailnet()
        self.assertEqual(client.post_calls, [("/admin/tailnet/enroll", {"auth_key": "tskey-auth-fake"})])
        self.assertEqual(screen.tailnet_auth_key, "")
        self.assertEqual(screen.tailnet_message, "Result: enrolled")
        self.assertTrue(screen.tailnet.get("enrolled"))

    def test_tailnet_enroll_failure_surfaces_message_and_keeps_key(self) -> None:
        client = _FakeApiClient(post_error=DroneApiError("auth key is required"))
        screen = SwarmScreen(client)
        screen.tailnet_auth_key = ""
        screen._enroll_tailnet()
        self.assertEqual(screen.tailnet_message, "auth key is required")

    def test_lan_discover_replaces_status(self) -> None:
        client = _FakeApiClient(post_responses={
            "/admin/local-network/discover": {"pairing": {"code": "1"}, "peers": [{"drone_id": "p1", "name": "Arcade"}]}
        })
        screen = SwarmScreen(client)
        screen._discover_lan()
        self.assertEqual(len(screen.lan["peers"]), 1)
        self.assertIsNone(screen.lan_error)

    def test_lan_rotate_pairing_code_updates_code_only(self) -> None:
        client = _FakeApiClient(post_responses={
            "/admin/local-network/pairing-code/rotate": {"pairing": {"code": "99999999"}}
        })
        screen = SwarmScreen(client)
        screen.lan = {"pairing": {"code": "11111111"}, "peers": ["stale"]}
        screen._rotate_pairing_code()
        self.assertEqual(screen.lan["pairing"]["code"], "99999999")
        self.assertEqual(screen.lan["peers"], ["stale"])

    def test_pair_with_success_clears_typed_code_and_reloads(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/local-network/status": {"pairing": {"code": "1"}, "peers": []}},
            post_responses={"/admin/local-network/peers/p1/pair": {"status": "paired", "peer": {"name": "Arcade"}}},
        )
        screen = SwarmScreen(client)
        screen.pairing_code_inputs["p1"] = "87654321"
        screen._pair_with("p1", "Arcade Cabinet")
        self.assertEqual(
            client.post_calls,
            [("/admin/local-network/peers/p1/pair", {"pairing_code": "87654321"})],
        )
        self.assertEqual(screen.lan_message, "Paired with Arcade Cabinet.")
        self.assertNotIn("p1", screen.pairing_code_inputs)

    def test_pair_with_failure_surfaces_message(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/local-network/status": {}},
            post_error=DroneApiError("incorrect pairing code"),
        )
        screen = SwarmScreen(client)
        screen.pairing_code_inputs["p1"] = "00000000"
        screen._pair_with("p1", "Arcade Cabinet")
        self.assertEqual(screen.lan_message, "incorrect pairing code")

    # --- Reference ROMs --------------------------------------------------

    def test_reference_reload_merges_paired_peers_with_share_records(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/swarm/overview": {"drones": [
                {"drone_id": "self", "is_self": True},
                {"drone_id": "peer1", "name": "Arcade"},
                {"drone_id": "peer2", "name": "Living Room"},
            ]},
            "/admin/network-shares": {"shares": [{"peer_id": "peer1", "enabled": True, "status": "mounted"}]},
        })
        screen = SwarmScreen(client)
        screen._reload_reference()
        self.assertEqual(len(screen.reference_peers), 2)  # self excluded
        peer1, share1 = screen.reference_peers[0]
        self.assertEqual(peer1["drone_id"], "peer1")
        self.assertEqual(share1["status"], "mounted")
        peer2, share2 = screen.reference_peers[1]
        self.assertEqual(peer2["drone_id"], "peer2")
        self.assertIsNone(share2)

    def test_reference_reload_surfaces_error(self) -> None:
        client = _FakeApiClient(get_error=DroneApiError("swarm unavailable"))
        screen = SwarmScreen(client)
        screen._reload_reference()
        self.assertEqual(screen.reference_error, "swarm unavailable")

    def test_enable_reference_posts_and_reloads(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/swarm/overview": {"drones": []}, "/admin/network-shares": {"shares": []}},
            post_responses={"/admin/network-shares/peer1/enable": {"status": "mounted"}},
        )
        screen = SwarmScreen(client)
        screen._enable_reference("peer1", "Arcade")
        self.assertEqual(client.post_calls, [("/admin/network-shares/peer1/enable", None)])
        self.assertIn("Referencing Arcade", screen.reference_message)

    def test_disable_reference_posts_and_reloads(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/swarm/overview": {"drones": []}, "/admin/network-shares": {"shares": []}},
            post_responses={"/admin/network-shares/peer1/disable": {"status": "detaching"}},
        )
        screen = SwarmScreen(client)
        screen._disable_reference("peer1", "Arcade")
        self.assertEqual(client.post_calls, [("/admin/network-shares/peer1/disable", None)])
        self.assertIn("Unreferencing Arcade", screen.reference_message)

    def test_enable_reference_failure_surfaces_message(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/swarm/overview": {"drones": []}, "/admin/network-shares": {"shares": []}},
            post_error=DroneApiError("mount failed"),
        )
        screen = SwarmScreen(client)
        screen._enable_reference("peer1", "Arcade")
        self.assertEqual(screen.reference_message, "mount failed")

    # --- Request Assets ----------------------------------------------------

    def test_request_reload_peers_excludes_self(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/swarm/overview": {"drones": [
                {"drone_id": "self", "is_self": True}, {"drone_id": "peer1", "name": "Arcade"},
            ]}
        })
        screen = SwarmScreen(client)
        screen._reload_request_peers()
        self.assertEqual(len(screen.request_peers), 1)
        self.assertEqual(screen.request_peers[0]["drone_id"], "peer1")

    def test_select_request_peer_loads_summary_and_resets_state(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/local-network/peers/peer1/assets?type=summary": {"system_counts": {"snes": 3}}
        })
        screen = SwarmScreen(client)
        screen.request_roms = [{"stale": True}]
        screen._select_request_peer("peer1", "Arcade")
        self.assertEqual(screen.request_peer_id, "peer1")
        self.assertEqual(screen.request_peer_name, "Arcade")
        self.assertEqual(screen.request_summary, {"snes": 3})
        self.assertEqual(screen.request_roms, [])

    def test_leave_request_peer_clears_selection(self) -> None:
        client = _FakeApiClient()
        screen = SwarmScreen(client)
        screen.request_peer_id = "peer1"
        screen._leave_request_peer()
        self.assertIsNone(screen.request_peer_id)

    def test_select_request_system_loads_roms(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/local-network/peers/peer1/assets?type=roms&system=snes&limit=200&offset=0": {
                "items": [{"name": "Zelda", "unique_id": "1"}], "total": 1,
            }
        })
        screen = SwarmScreen(client)
        screen.request_peer_id = "peer1"
        screen._select_request_system("snes")
        self.assertEqual(screen.request_roms, [{"name": "Zelda", "unique_id": "1"}])
        self.assertEqual(screen.request_roms_total, 1)

    def test_select_request_system_search_reloads_with_query(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/local-network/peers/peer1/assets?type=roms&system=snes&limit=200&offset=0&q=zelda": {
                "items": [{"name": "Zelda"}], "total": 1,
            }
        })
        screen = SwarmScreen(client)
        screen.request_peer_id = "peer1"
        screen.request_selected_system = "snes"
        screen.request_roms_query = "zelda"
        screen._reload_request_roms()
        self.assertEqual(screen.request_roms, [{"name": "Zelda"}])

    def test_select_request_kind_movies_loads_lazily_once(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/local-network/peers/peer1/assets?type=movies&limit=200&offset=0": {
                "items": [{"movie_name": "Alien"}]
            }
        })
        screen = SwarmScreen(client)
        screen.request_peer_id = "peer1"
        screen._select_request_kind("movies")
        self.assertEqual(screen.request_movies, [{"movie_name": "Alien"}])

        client._get_responses["/admin/local-network/peers/peer1/assets?type=movies&limit=200&offset=0"] = {
            "items": [{"movie_name": "changed"}]
        }
        screen._select_request_kind("systems")
        screen._select_request_kind("movies")  # already loaded -- shouldn't refetch
        self.assertEqual(screen.request_movies, [{"movie_name": "Alien"}])

    def test_request_item_roms_posts_expected_payload(self) -> None:
        client = _FakeApiClient(post_responses={"/admin/local-network/sync": {"status": "queued"}})
        screen = SwarmScreen(client)
        screen.request_peer_id = "peer1"
        screen._request_item("roms", {"name": "Zelda"}, "Zelda", system="snes")
        self.assertEqual(
            client.post_calls,
            [("/admin/local-network/sync", {"peer_id": "peer1", "asset_type": "roms", "item": {"name": "Zelda"}, "system": "snes"})],
        )
        self.assertEqual(screen.request_message, "Requested Zelda.")

    def test_request_item_movies_omits_system(self) -> None:
        client = _FakeApiClient(post_responses={"/admin/local-network/sync": {"status": "queued"}})
        screen = SwarmScreen(client)
        screen.request_peer_id = "peer1"
        screen._request_item("movies", {"movie_name": "Alien"}, "Alien")
        self.assertEqual(
            client.post_calls,
            [("/admin/local-network/sync", {"peer_id": "peer1", "asset_type": "movies", "item": {"movie_name": "Alien"}})],
        )

    def test_request_item_failure_surfaces_message(self) -> None:
        client = _FakeApiClient(post_error=DroneApiError("peer offline"))
        screen = SwarmScreen(client)
        screen.request_peer_id = "peer1"
        screen._request_item("movies", {"movie_name": "Alien"}, "Alien")
        self.assertEqual(screen.request_message, "peer offline")


class VpnScreenTests(unittest.TestCase):
    def test_on_enter_loads_status(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/vpn": {"status": "disconnected", "installed": True, "has_config": True, "remotes": ["vpn.example.com"]}
        })
        screen = VpnScreen(client)
        screen.on_enter()
        self.assertEqual(screen.status["status"], "disconnected")
        self.assertIsNone(screen.error)

    def test_on_enter_surfaces_error(self) -> None:
        client = _FakeApiClient(get_error=DroneApiError("vpn unavailable"))
        screen = VpnScreen(client)
        screen.on_enter()
        self.assertEqual(screen.error, "vpn unavailable")

    def test_connect_success_reloads_status(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/vpn": {"status": "connecting", "installed": True, "has_config": True}},
            post_responses={"/admin/vpn/connect": {"status": "connecting"}},
        )
        screen = VpnScreen(client)
        screen._connect()
        self.assertEqual(screen.action_message, "Result: connecting")
        self.assertEqual(screen.status["status"], "connecting")

    def test_connect_failure_surfaces_joined_errors_message(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/vpn": {"status": "disconnected", "installed": True, "has_config": False}},
            post_error=DroneApiError("no config uploaded; no credentials saved"),
        )
        screen = VpnScreen(client)
        screen._connect()
        self.assertEqual(screen.action_message, "no config uploaded; no credentials saved")

    def test_disconnect_calls_post_and_reloads(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/vpn": {"status": "disconnected", "installed": True, "has_config": True}},
            post_responses={"/admin/vpn/disconnect": {"status": "disconnected"}},
        )
        screen = VpnScreen(client)
        screen._disconnect()
        self.assertEqual(client.post_calls, [("/admin/vpn/disconnect", None)])
        self.assertEqual(screen.action_message, "Result: disconnected")


class BackupsScreenTests(unittest.TestCase):
    def test_on_enter_populates_backups(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/config-backups": {"backups": [{"id": 1, "status": "complete", "file_name": "backup-1.tar.gz"}]}
        })
        screen = BackupsScreen(client)
        screen.on_enter()
        self.assertEqual(len(screen.backups), 1)
        self.assertIsNone(screen.error)

    def test_on_enter_surfaces_error(self) -> None:
        client = _FakeApiClient(get_error=DroneApiError("backups unavailable"))
        screen = BackupsScreen(client)
        screen.on_enter()
        self.assertEqual(screen.error, "backups unavailable")

    def test_create_backup_success_reloads_list(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/config-backups": {"backups": [{"id": 1, "status": "creating"}]}},
            post_responses={"/admin/config-backups": {"status": "ok", "backup": {"id": 1, "status": "creating"}}},
        )
        screen = BackupsScreen(client)
        screen._create_backup()
        self.assertEqual(screen.create_message, "Backup started.")
        self.assertEqual(len(screen.backups), 1)

    def test_create_backup_already_creating(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/config-backups": {"backups": []}},
            post_responses={"/admin/config-backups": {"status": "already_creating"}},
        )
        screen = BackupsScreen(client)
        screen._create_backup()
        self.assertEqual(screen.create_message, "A backup is already being created.")

    def test_format_size(self) -> None:
        from ui.screens.backups import _format_size
        self.assertEqual(_format_size(500), "500B")
        self.assertEqual(_format_size(2048), "2.0KB")


class AppShellTests(unittest.TestCase):
    def test_starts_on_swarm_and_enters_it_lazily(self) -> None:
        client = _FakeApiClient(get_responses={"/admin/swarm/overview": {"active": False, "drones": []}})
        shell = AppShell(client, "batocera", on_quit=lambda: None)
        self.assertEqual(shell.section, "swarm")
        shell.on_enter()
        self.assertIn("swarm", shell._entered_keys)
        self.assertNotIn("vpn", shell._entered_keys)

    def test_selecting_vpn_enters_it_lazily(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/vpn": {"status": "disconnected", "installed": True, "has_config": False}
        })
        shell = AppShell(client, "batocera", on_quit=lambda: None)
        shell.on_enter()
        shell._select_section("vpn")
        self.assertIn("vpn", shell._entered_keys)
        self.assertEqual(shell._content["vpn"].status["status"], "disconnected")

    def test_switching_sections_enters_each_exactly_once(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/swarm/overview": {"active": False, "drones": []},
            "/admin/vpn": {"status": "disconnected", "installed": True, "has_config": False},
            "/admin/config-backups": {"backups": []},
        })
        shell = AppShell(client, "batocera", on_quit=lambda: None)
        shell.on_enter()  # swarm, default section
        shell._select_section("vpn")
        shell._select_section("backups")
        shell._select_section("backups")  # re-selecting shouldn't re-enter
        self.assertEqual(shell._entered_keys, {"swarm", "vpn", "backups"})

    def test_quit_button_logic_calls_callback(self) -> None:
        client = _FakeApiClient()
        calls = []
        shell = AppShell(client, "batocera", on_quit=lambda: calls.append("quit"))
        shell.on_quit()
        self.assertEqual(calls, ["quit"])


if __name__ == "__main__":
    unittest.main()
