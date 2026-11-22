"""Unit tests for screen orchestration logic (session resume, API-error handling,
navigation callbacks) -- the parts of each screen that don't call into imgui's
drawing functions, which need a live render frame this suite doesn't set up.
See test_http_client.py / test_http_client_integration.py for the HTTP layer.
"""

import threading
import unittest

from client.errors import AuthenticationError, DroneApiError
from ui.screens.about import AboutScreen
from ui.screens.backups import BackupsScreen
from ui.screens.base import Screen
from ui.screens.login import LoginScreen
from ui.screens.swarm import (
    SwarmScreen,
    _download_job_label,
    _drone_status_text,
    _item_metadata_label,
)
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
        self.get_calls = []
        self.post_calls = []

    def session_status(self):
        return self._session_status

    def login(self, username, password):
        self.login_calls.append((username, password))
        if self._login_error is not None:
            raise self._login_error
        return self._login_result or username

    def get(self, path, *, timeout=None):
        self.get_calls.append(path)
        if self._get_error is not None:
            raise self._get_error
        key = path if path in self._get_responses else path.split("?", 1)[0]
        response = self._get_responses.get(key, {})
        if isinstance(response, list):
            return response.pop(0) if response else {}
        return response

    def post(self, path, body=None, *, timeout=None):
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
        self.assertIn("/admin/swarm/overview?probe=0", client.get_calls)

    def test_overview_probes_each_peer_without_holding_the_loading_panel(self) -> None:
        class _GatedClient(_FakeApiClient):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.release_probes = threading.Event()

            def get(self, path, *, timeout=None):
                if path.endswith("/probe"):
                    self.release_probes.wait(2)
                return super().get(path, timeout=timeout)

        client = _GatedClient(get_responses={
            "/admin/swarm/overview?probe=0": {
                "active": True,
                "drones": [
                    {"drone_id": "self", "is_self": True, "online": True},
                    {"drone_id": "peer1", "name": "Arcade", "pending": True, "online": None},
                ],
            },
            "/admin/swarm/peers/peer1/probe": {
                "drone": {"drone_id": "peer1", "name": "Arcade", "online": True, "pending": False},
            },
        })
        screen = SwarmScreen(client)
        screen._reload_overview()
        self.assertIsNone(screen.deferred_action_label)
        self.assertEqual(client.get_calls, ["/admin/swarm/overview?probe=0"])
        pending = next(drone for drone in screen.drones if drone.get("drone_id") == "peer1")
        self.assertEqual(_drone_status_text(pending), "Checking...")
        client.release_probes.set()
        screen.wait_for_peer_probes()
        resolved = next(drone for drone in screen.drones if drone.get("drone_id") == "peer1")
        self.assertTrue(resolved.get("online"))
        self.assertFalse(resolved.get("pending"))
        self.assertEqual(_drone_status_text(resolved), "Online")
        self.assertIn("/admin/swarm/peers/peer1/probe", client.get_calls)

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

    # --- Search Assets -----------------------------------------------------

    def test_search_loads_sources_and_all_systems_roms(self) -> None:
        client = _FakeApiClient(get_responses={
            "/media-browse/sources": {
                "sources": [
                    {"drone_id": "self", "name": "This Drone", "is_local": True, "online": True},
                    {"drone_id": "peer1", "name": "Arcade", "is_local": False, "online": True},
                ]
            },
            "/media-browse/roms": {
                "items": [{"name": "Zelda", "system": "snes", "drone_id": "peer1", "drone_name": "Arcade"}],
                "sources": [{"drone_id": "peer1", "name": "Arcade", "status": "ok"}],
                "next_cursor": "cursor-2",
                "has_more": True,
            },
        })
        screen = SwarmScreen(client)
        screen._reload_search()
        self.assertEqual(len(screen.browse_items), 1)
        self.assertEqual(screen.browse_items[0]["name"], "Zelda")
        self.assertEqual(screen.available_systems, ["snes"])
        self.assertEqual(screen.next_page_token, "cursor-2")
        self.assertTrue(screen.has_more_pages)

    def test_search_load_next_page_appends_items(self) -> None:
        client = _FakeApiClient(get_responses={
            "/media-browse/roms": [
                {
                    "items": [{"name": "Zelda", "system": "snes", "drone_id": "peer1"}],
                    "sources": [],
                    "next_cursor": "cursor-2",
                    "has_more": True,
                },
                {
                    "items": [{"name": "Metroid", "system": "nes", "drone_id": "peer1"}],
                    "sources": [],
                    "next_cursor": "",
                    "has_more": False,
                },
            ],
        })
        screen = SwarmScreen(client)
        screen._reload_search()
        screen._load_next_page()
        self.assertEqual([row["name"] for row in screen.browse_items], ["Zelda", "Metroid"])
        self.assertFalse(screen.has_more_pages)

    def test_sub_tab_triggers_step_asset_kinds_then_overflow_to_swarm_tabs(self) -> None:
        from ui.screens import swarm as swarm_module

        screen = SwarmScreen(_FakeApiClient())
        screen._loaded_tabs.update(key for key, _label in swarm_module._TABS)
        screen.tab = swarm_module._TAB_SEARCH
        screen.asset_kind = swarm_module._SEARCH_KIND_MUSIC
        screen._step_sub_tab(1)  # past the last asset kind -> next Swarm tab
        self.assertEqual(screen.tab, swarm_module._TAB_OVERVIEW)
        screen.tab = swarm_module._TAB_SEARCH
        screen._step_sub_tab(-1)
        self.assertEqual(screen.asset_kind, swarm_module._SEARCH_KIND_MOVIES)
        self.assertEqual(screen.tab, swarm_module._TAB_SEARCH)

    def test_request_item_runs_in_background_not_in_click_frame(self) -> None:
        client = _FakeApiClient(post_responses={"/admin/local-network/sync": {"status": "queued"}})
        screen = SwarmScreen(client)
        screen._request_item("roms", {"name": "Zelda", "drone_id": "peer1"}, "Zelda", system="snes")
        self.assertEqual(client.post_calls, [])
        self.assertEqual(screen._pending_request_key, ("roms", "Zelda"))
        self.assertEqual(screen.deferred_action_label, "Starting download: Zelda...")
        screen.wait_for_deferred_action()
        self.assertEqual(len(client.post_calls), 1)
        self.assertIsNone(screen._pending_request_key)

    def test_hide_local_items_filters_downloaded_items(self) -> None:
        screen = SwarmScreen(_FakeApiClient())
        screen.browse_items = [
            {"name": "Local", "exists_locally": True},
            {"name": "Remote", "exists_locally": False},
            {"name": "Just finished", "_downloaded": True},
        ]
        screen.hide_local_items = True
        self.assertEqual(
            [row["name"] for row in screen._visible_browse_items()],
            ["Remote"],
        )
        screen.hide_local_items = False
        self.assertEqual(len(screen._visible_browse_items()), 3)

    def test_download_poll_updates_queue_snapshot_asynchronously(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/downloads": {
                "active": [{
                    "job_id": "job-1", "status": "downloading",
                    "file_name": "Zelda.zip", "downloaded_bytes": 25,
                    "total_bytes": 100, "percentage": 25,
                }],
                "queued": [{"job_id": "job-2", "status": "queued"}],
                "recent": [],
                "downloads": [],
            }
        })
        screen = SwarmScreen(client)
        screen._maybe_poll_downloads()
        poll = screen._download_poll_thread
        self.assertIsNotNone(poll)
        poll.join(1)
        screen._maybe_poll_downloads()
        self.assertEqual(len(screen.request_queue_snapshot["active"]), 1)
        self.assertEqual(len(screen.request_queue_snapshot["queued"]), 1)
        self.assertIsNone(screen._download_poll_thread)

    def test_download_queue_rows_group_active_queued_and_recent(self) -> None:
        screen = SwarmScreen(_FakeApiClient())
        screen.request_queue_snapshot = {
            "active": [{"job_id": "active"}],
            "queued": [{"job_id": "queued"}],
            "recent": [{"job_id": "recent"}],
        }
        self.assertEqual(
            [(group, job["job_id"]) for group, job in screen._download_queue_rows()],
            [("Active", "active"), ("Queued", "queued"), ("Recent", "recent")],
        )

    def test_download_queue_rows_put_newest_download_first_across_statuses(self) -> None:
        screen = SwarmScreen(_FakeApiClient())
        screen.request_queue_snapshot = {
            "active": [{"job_id": "old-active", "created_at": "2026-08-14T10:00:00+00:00"}],
            "queued": [{"job_id": "new-queued", "created_at": "2026-08-14T10:02:00+00:00"}],
            "recent": [{"job_id": "middle-recent", "created_at": "2026-08-14T10:01:00+00:00"}],
        }
        self.assertEqual(
            [(group, job["job_id"]) for group, job in screen._download_queue_rows()],
            [
                ("Queued", "new-queued"),
                ("Recent", "middle-recent"),
                ("Active", "old-active"),
            ],
        )

    def test_download_queue_labels_distinguish_game_and_artwork_jobs(self) -> None:
        self.assertEqual(
            _download_job_label({"file_type": "ROM", "file_name": "Zelda.zip"}),
            "[Game] Zelda.zip",
        )
        self.assertEqual(
            _download_job_label({
                "file_type": "ARTWORK",
                "file_name": "Zelda.zip",
                "artwork_type": "image",
            }),
            "[Image artwork] Zelda.zip",
        )
        self.assertEqual(
            _download_job_label({
                "file_type": "ARTWORK",
                "rom_name": "Zelda.zip",
                "artwork_type": "box_front",
            }),
            "[Box Front artwork] Zelda.zip",
        )

    def test_do_request_item_roms_posts_expected_payload(self) -> None:
        client = _FakeApiClient(post_responses={"/admin/local-network/sync": {"status": "queued"}})
        screen = SwarmScreen(client)
        screen._do_request_item("roms", {"name": "Zelda", "drone_id": "peer1"}, "Zelda", "snes")
        self.assertEqual(
            client.post_calls,
            [(
                "/admin/local-network/sync",
                {
                    "peer_id": "peer1",
                    "asset_type": "roms",
                    "item": {"name": "Zelda", "drone_id": "peer1"},
                    "include_artwork": True,
                    "system": "snes",
                },
            )],
        )
        self.assertEqual(screen.request_message, "Download started: Zelda.")

    def test_do_request_item_movies_omits_system(self) -> None:
        client = _FakeApiClient(post_responses={"/admin/local-network/sync": {"status": "queued"}})
        screen = SwarmScreen(client)
        screen._do_request_item("movies", {"movie_name": "Alien", "drone_id": "peer1"}, "Alien", "")
        self.assertEqual(
            client.post_calls,
            [(
                "/admin/local-network/sync",
                {
                    "peer_id": "peer1",
                    "asset_type": "movies",
                    "item": {"movie_name": "Alien", "drone_id": "peer1"},
                    "include_artwork": True,
                },
            )],
        )

    def test_do_request_item_failure_surfaces_message(self) -> None:
        client = _FakeApiClient(post_error=DroneApiError("peer offline"))
        screen = SwarmScreen(client)
        screen._do_request_item("movies", {"movie_name": "Alien", "drone_id": "peer1"}, "Alien", "")
        self.assertEqual(screen.request_message, "peer offline")

    def test_ignore_existing_games_defaults_checked(self) -> None:
        screen = SwarmScreen(_FakeApiClient())
        self.assertTrue(screen.ignore_existing_games)

    def test_unchecked_ignore_existing_requests_overwrite(self) -> None:
        client = _FakeApiClient(post_responses={
            "/admin/local-network/sync": {
                "status": "queued",
                "jobs": [{"job_id": "job-1", "file_type": "ROM", "status": "queued"}],
            }
        })
        screen = SwarmScreen(client)
        screen.ignore_existing_games = False
        screen.hide_local_items = False
        screen._do_request_item("roms", {"name": "Zelda", "drone_id": "peer1"}, "Zelda", "snes")
        self.assertEqual(client.post_calls[0][1]["overwrite_files"], True)

    def test_download_progress_updates_row_and_marks_completion(self) -> None:
        screen = SwarmScreen(_FakeApiClient())
        row = {"name": "Zelda", "relative_path": "Zelda.zip"}
        screen.browse_items = [row]
        key = ("roms", "Zelda.zip")
        screen._track_download(
            key,
            "Zelda",
            [{"job_id": "job-1", "file_type": "ROM", "status": "queued", "total_bytes": 100}],
        )
        screen._apply_download_snapshot({
            "downloads": [{
                "job_id": "job-1", "file_type": "ROM", "status": "downloading",
                "downloaded_bytes": 40, "total_bytes": 100,
            }]
        })
        self.assertEqual(screen.request_downloads[key]["status"], "downloading")
        self.assertEqual(screen.request_downloads[key]["percentage"], 40.0)

        screen._apply_download_snapshot({
            "downloads": [{
                "job_id": "job-1", "file_type": "ROM", "status": "completed",
                "downloaded_bytes": 100, "total_bytes": 100,
            }]
        })
        self.assertEqual(screen.request_downloads[key]["message"], "Downloaded")
        self.assertTrue(row["exists_locally"])
        self.assertTrue(row["_downloaded"])

    def test_download_all_uses_bulk_endpoint_and_tracks_new_jobs(self) -> None:
        before = {"downloads": [{"job_id": "old-job", "source_drone_id": "peer1", "file_type": "ROM"}]}
        after = {"downloads": [
            {"job_id": "old-job", "source_drone_id": "peer1", "file_type": "ROM"},
        ]}
        client = _FakeApiClient(
            get_responses={"/admin/downloads": [before, after]},
            post_responses={"/admin/local-network/sync-bulk": {
                "status": "queued", "queued_assets": 1, "queued_artwork": 0,
                "queued_job_ids": ["new-job"],
                "queued_jobs": [{
                    "job_id": "new-job", "source_drone_id": "peer1", "file_type": "ROM",
                    "relative_path": "Zelda.zip", "file_name": "Zelda.zip", "status": "queued",
                }],
                "skipped_existing": 2, "total_available": 3,
            }},
        )
        screen = SwarmScreen(client)
        screen.browse_items = [{"name": "Zelda", "drone_id": "peer1", "system": "snes"}]
        screen.system_filter = "snes"
        screen._download_all()
        self.assertEqual(
            client.post_calls,
            [("/admin/local-network/sync-bulk", {
                "peer_id": "peer1", "asset_type": "roms", "system": "snes",
            })],
        )
        self.assertEqual(screen.request_batch["job_ids"], ["new-job"])
        self.assertIn(("roms", "Zelda.zip"), screen.request_downloads)
        self.assertEqual(screen.request_downloads[("roms", "Zelda.zip")]["status"], "completed")
        self.assertIn("ignored 2", screen.request_message)

    def test_download_all_unchecked_existing_enables_overwrite(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/downloads": [{"downloads": []}, {"downloads": []}]},
            post_responses={"/admin/local-network/sync-bulk": {"queued_assets": 0}},
        )
        screen = SwarmScreen(client)
        screen.browse_items = [{"name": "Zelda", "drone_id": "peer1", "system": "snes"}]
        screen.system_filter = "snes"
        screen.ignore_existing_games = False
        screen.hide_local_items = False
        screen._download_all()
        self.assertTrue(client.post_calls[0][1]["overwrite_files"])


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

    # --- Credentials -------------------------------------------------------

    def test_save_credentials_success_clears_password_and_reloads(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/vpn": {"status": "disconnected", "installed": True, "has_config": True}},
            post_responses={"/admin/vpn/credentials": {"status": "ok", "username": "alice"}},
        )
        screen = VpnScreen(client)
        screen.username_input = "alice"
        screen.password_input = "hunter2"
        screen._save_credentials()
        self.assertEqual(
            client.post_calls, [("/admin/vpn/credentials", {"username": "alice", "password": "hunter2"})]
        )
        self.assertEqual(screen.credentials_message, "Saved credentials for alice.")
        self.assertEqual(screen.password_input, "")

    def test_save_credentials_failure_still_clears_password(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/vpn": {"status": "disconnected", "installed": True, "has_config": True}},
            post_error=DroneApiError("password is required"),
        )
        screen = VpnScreen(client)
        screen.password_input = "hunter2"
        screen._save_credentials()
        self.assertEqual(screen.credentials_message, "password is required")
        self.assertEqual(screen.password_input, "")

    # --- Sharing -------------------------------------------------------------

    def test_set_sharing_success(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/vpn": {"status": "disconnected", "installed": True, "has_config": True}},
            post_responses={"/admin/vpn/sharing": {"status": "ok", "sharing_enabled": True}},
        )
        screen = VpnScreen(client)
        screen._set_sharing(True)
        self.assertEqual(client.post_calls, [("/admin/vpn/sharing", {"enabled": True})])
        self.assertEqual(screen.sharing_message, "VPN sharing enabled.")

    def test_set_sharing_never_calls_the_endpoint_when_config_was_imported(self) -> None:
        # The single-hop-only rule: an imported config can never be
        # re-shared. This must be enforced here, not just hidden behind a
        # draw()-time conditional -- state-level, no live imgui frame needed.
        client = _FakeApiClient(
            get_responses={
                "/admin/vpn": {
                    "status": "disconnected", "installed": True, "has_config": True,
                    "source_peer_id": "peer-1", "source_peer_name": "Living Room",
                }
            },
        )
        screen = VpnScreen(client)
        screen.on_enter()
        screen._set_sharing(True)
        self.assertEqual(client.post_calls, [])
        self.assertIsNone(screen.sharing_message)

    # --- Get a config: pull from a paired peer --------------------------------

    def test_reload_pull_peers_excludes_self_and_offline(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/swarm/overview": {"drones": [
                {"drone_id": "self", "is_self": True, "online": True},
                {"drone_id": "peer1", "name": "Arcade", "online": True},
                {"drone_id": "peer2", "name": "Offline Peer", "online": False},
            ]}
        })
        screen = VpnScreen(client)
        screen._reload_pull_peers()
        self.assertEqual(len(screen.pull_peers), 1)
        self.assertEqual(screen.pull_peers[0]["drone_id"], "peer1")

    def test_pull_from_peer_success_with_credentials(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/vpn": {"status": "disconnected", "installed": True, "has_config": True}},
            post_responses={
                "/admin/vpn/pull-from-peer": {"status": "ok", "config_filename": "provider.ovpn", "credentials_imported": True}
            },
        )
        screen = VpnScreen(client)
        screen._pull_from_peer("peer1", "Arcade")
        self.assertEqual(client.post_calls, [("/admin/vpn/pull-from-peer", {"peer_id": "peer1"})])
        self.assertEqual(screen.pull_message, "Pulled config and credentials from Arcade.")

    def test_pull_from_peer_failure_surfaces_message(self) -> None:
        client = _FakeApiClient(post_error=DroneApiError("That drone is not a paired peer."))
        screen = VpnScreen(client)
        screen._pull_from_peer("peer1", "Arcade")
        self.assertEqual(screen.pull_message, "That drone is not a paired peer.")

    # --- Get a config: import from the local drop folder ----------------------

    def test_reload_import_files_populates_files_and_directory(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/vpn/import-folder": {"directory": "/userdata/vpn-import", "files": ["provider.ovpn"]}
        })
        screen = VpnScreen(client)
        screen._reload_import_files()
        self.assertEqual(screen.import_files, ["provider.ovpn"])
        self.assertEqual(screen.import_directory, "/userdata/vpn-import")
        self.assertIsNone(screen.import_files_error)

    def test_reload_import_files_surfaces_error(self) -> None:
        client = _FakeApiClient(get_error=DroneApiError("boom"))
        screen = VpnScreen(client)
        screen._reload_import_files()
        self.assertEqual(screen.import_files_error, "boom")

    def test_import_from_folder_success(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/vpn": {"status": "disconnected", "installed": True, "has_config": True}},
            post_responses={
                "/admin/vpn/import-folder/apply": {"status": "ok", "config_filename": "provider.ovpn", "remotes": []}
            },
        )
        screen = VpnScreen(client)
        screen._import_from_folder("provider.ovpn")
        self.assertEqual(
            client.post_calls, [("/admin/vpn/import-folder/apply", {"filename": "provider.ovpn"})]
        )
        self.assertEqual(screen.import_message, "Imported provider.ovpn.")

    def test_import_from_folder_failure_surfaces_backend_error(self) -> None:
        client = _FakeApiClient(post_error=DroneApiError("File not found in the drop folder"))
        screen = VpnScreen(client)
        screen._import_from_folder("missing.ovpn")
        self.assertEqual(screen.import_message, "File not found in the drop folder")

    def test_select_get_config_mode_loads_each_mode_exactly_once(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/swarm/overview": {"drones": []},
            "/admin/vpn/import-folder": {"directory": "/userdata/vpn-import", "files": []},
        })
        screen = VpnScreen(client)
        screen._select_get_config_mode("peer")
        screen._select_get_config_mode("folder")
        screen._select_get_config_mode("peer")  # re-selecting shouldn't reload
        self.assertEqual(screen._get_config_loaded, {"peer", "folder"})


_BACKUP_ID = "a" * 32
_CATEGORIES = [
    {"id": "batocera", "label": "Batocera", "group": "batocera", "group_label": "Batocera", "file_count": 1},
    {"id": "saves:snes", "label": "Saves: snes", "group": "saves", "group_label": "Saves", "file_count": 3},
]


class BackupsScreenTests(unittest.TestCase):
    def _local(self, **extra):
        row = {"backup_id": _BACKUP_ID, "name": "Known good", "status": "complete", "kind": "backup",
               "categories": _CATEGORIES, "size_bytes": 2048}
        row.update(extra)
        return row

    def test_on_enter_loads_local_and_swarm_backups(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/backups": {"backups": [self._local()], "snapshots": [], "restore": {"state": "idle"}},
            "/admin/backups/swarm": {"sources": [
                {"drone_id": "peer-1", "name": "Den", "status": "ok", "backups": [
                    {"backup_id": "b" * 32, "drone_id": "peer-1", "name": "Den setup", "categories": _CATEGORIES}]},
                {"drone_id": "peer-2", "name": "Attic", "status": "error", "error": "offline", "backups": []},
            ]},
        })
        screen = BackupsScreen(client)
        screen.on_enter()
        self.assertEqual(len(screen.local_backups), 1)
        self.assertEqual([source["drone_id"] for source in screen.sources], ["peer-1", "peer-2"])
        self.assertIsNone(screen.error)
        self.assertTrue(any(call.startswith("/admin/backups/swarm?") for call in client.get_calls))

    def test_on_enter_surfaces_error(self) -> None:
        screen = BackupsScreen(_FakeApiClient(get_error=DroneApiError("backups unavailable")))
        screen.on_enter()
        self.assertEqual(screen.error, "backups unavailable")

    def test_search_sends_query(self) -> None:
        client = _FakeApiClient(get_responses={"/admin/backups/swarm": {"sources": []}})
        screen = BackupsScreen(client)
        screen.query = "retroarch shaders"
        screen._search()
        self.assertIn("q=retroarch+shaders", client.get_calls[-1])

    def test_create_backup_posts_and_reloads(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/backups": {"backups": [self._local(status="creating")], "snapshots": []}},
            post_responses={"/admin/backups": {"status": "started"}},
        )
        screen = BackupsScreen(client)
        screen._create_backup()
        self.assertEqual(client.post_calls[0][0], "/admin/backups")
        self.assertIn("Backup started", screen.message)
        self.assertEqual(len(screen.local_backups), 1)

    def test_copy_then_restore_opens_dialog_when_transfer_completes(self) -> None:
        peer_id = "b" * 32
        client = _FakeApiClient(
            get_responses={
                "/admin/downloads": {"active": [], "queued": [], "recent": [{"id": "job-1", "status": "completed", "percentage": 100}]},
                "/admin/backups": {"backups": [self._local(backup_id=peer_id, name="Den setup")], "snapshots": []},
                "/admin/backups/swarm": {"sources": []},
            },
            post_responses={f"/admin/backups/swarm/peer-1/{peer_id}/download": {"status": "queued", "job": {"id": "job-1"}}},
        )
        screen = BackupsScreen(client)
        screen._poller = threading.current_thread()  # keep the test synchronous: no background poller
        screen._request_copy("peer-1", peer_id, restore_after=True)
        self.assertEqual(screen.copies[peer_id]["status"], "queued")
        self.assertFalse(screen._poll_downloads_once())
        self.assertEqual(screen.copies[peer_id]["status"], "completed")
        self.assertEqual(screen._open_after_copy, peer_id)

    def test_copy_failure_is_reported_on_the_row(self) -> None:
        client = _FakeApiClient(post_error=DroneApiError("That Drone is offline"))
        screen = BackupsScreen(client)
        screen._request_copy("peer-1", "b" * 32)
        self.assertEqual(screen.copies["b" * 32]["status"], "failed")
        self.assertIn("offline", screen.copies["b" * 32]["error"])

    def test_preview_and_restore_send_selected_categories_and_mode(self) -> None:
        client = _FakeApiClient(
            get_responses={"/admin/backups": {"backups": [], "snapshots": []}},
            post_responses={
                f"/admin/backups/{_BACKUP_ID}/plan": {"mode": "exact", "totals": {"new": 1, "changed": 2, "identical": 0, "remove": 1}, "warnings": []},
                f"/admin/backups/{_BACKUP_ID}/restore": {"state": "running"},
            },
        )
        screen = BackupsScreen(client)
        screen._open_restore(self._local())
        screen.dialog["selected"].discard("batocera")
        screen.dialog["mode"] = "exact"
        screen._preview()
        self.assertEqual(client.post_calls[0], (f"/admin/backups/{_BACKUP_ID}/plan",
                                                {"categories": ["saves:snes"], "mode": "exact", "include_machine_keys": False}))
        self.assertIsNotNone(screen.dialog["plan"])
        screen._restore()
        self.assertEqual(client.post_calls[1][1]["categories"], ["saves:snes"])
        self.assertFalse(client.post_calls[1][1]["acknowledge_warnings"])
        self.assertIsNone(screen.dialog)
        self.assertIn("safety snapshot", screen.message)

    def test_rollback_defaults_to_exact_and_includes_previously_empty_parts(self) -> None:
        screen = BackupsScreen(_FakeApiClient())
        snapshot = self._local(kind="snapshot", snapshot_of={"name": "Known good", "categories": ["batocera", "states:snes"]})
        screen._open_restore(snapshot, rollback=True)
        self.assertEqual(screen.dialog["mode"], "exact")
        self.assertIn("states:snes", screen.dialog["selected"])

    def test_restore_error_stays_in_dialog(self) -> None:
        client = _FakeApiClient(get_responses={"/admin/backups": {}}, post_error=DroneApiError("A restore is already running."))
        screen = BackupsScreen(client)
        screen._open_restore(self._local())
        screen._restore()
        self.assertIn("already running", screen.dialog["plan_error"])

    def test_choose_files_restores_only_picked_folders_and_files(self) -> None:
        from ui.screens.backups import build_file_trees, toggle_file, toggle_node
        files = [
            {"path": "saves/snes/Mario.srm", "category": "saves:snes", "size": 10},
            {"path": "saves/snes/Zelda.srm", "category": "saves:snes", "size": 10},
            {"path": "system/batocera.conf", "category": "batocera", "size": 5},
            {"path": "saves/yuzu/0000/USER/0100A/a.bin", "category": "saves:yuzu", "size": 7},
            {"path": "saves/yuzu/0000/USER/0100A/b.bin", "category": "saves:yuzu", "size": 7},
            {"path": "saves/yuzu/0000/USER/0100B/c.bin", "category": "saves:yuzu", "size": 7},
        ]
        trees = build_file_trees(files)
        yuzu = trees["saves:yuzu"]
        self.assertEqual(yuzu.path, "saves/yuzu/0000/USER")
        self.assertEqual(list(yuzu.dirs), ["0100A", "0100B"])

        categories = _CATEGORIES + [{"id": "saves:yuzu", "label": "Saves: yuzu", "group": "saves", "group_label": "Saves", "file_count": 3}]
        client = _FakeApiClient(
            get_responses={f"/admin/backups/{_BACKUP_ID}": {"files": files}, "/admin/backups": {}},
            post_responses={f"/admin/backups/{_BACKUP_ID}/plan": {"mode": "overlay", "totals": {}, "warnings": []},
                            f"/admin/backups/{_BACKUP_ID}/restore": {"state": "running"}},
        )
        screen = BackupsScreen(client)
        screen._open_restore(self._local(categories=categories))
        screen._load_files()
        dialog = screen.dialog
        self.assertIsNotNone(dialog["trees"])
        toggle_node(dialog, dialog["trees"]["saves:yuzu"], False)       # untick a whole part
        toggle_node(dialog, dialog["trees"]["saves:yuzu"].dirs["0100A"], True)  # tick one folder
        toggle_file(dialog, "saves:snes", "saves/snes/Zelda.srm", False)  # narrow a whole part
        screen._preview()
        body = client.post_calls[0][1]
        self.assertEqual(body["categories"], ["batocera"])
        self.assertEqual(sorted(body["paths"]), ["saves/snes/Mario.srm", "saves/yuzu/0000/USER/0100A"])
        screen._restore()
        self.assertEqual(sorted(client.post_calls[1][1]["paths"]), ["saves/snes/Mario.srm", "saves/yuzu/0000/USER/0100A"])

    def test_retick_every_file_restores_the_whole_part(self) -> None:
        from ui.screens.backups import build_file_trees, toggle_file
        files = [{"path": "saves/snes/Mario.srm", "category": "saves:snes", "size": 1},
                 {"path": "saves/snes/Zelda.srm", "category": "saves:snes", "size": 1}]
        screen = BackupsScreen(_FakeApiClient())
        screen._open_restore(self._local())
        screen.dialog["trees"] = build_file_trees(files)
        toggle_file(screen.dialog, "saves:snes", "saves/snes/Mario.srm", False)
        toggle_file(screen.dialog, "saves:snes", "saves/snes/Mario.srm", True)
        self.assertNotIn("paths", screen._dialog_options())
        self.assertIn("saves:snes", screen._dialog_options()["categories"])

    def test_format_size(self) -> None:
        from ui.screens.backups import _format_size
        self.assertEqual(_format_size(500), "500B")
        self.assertEqual(_format_size(2048), "2.0KB")

    def test_restore_wizard_also_fetch_games_and_media_step(self) -> None:
        from ui.screens.backups import _asset_space_warnings, asset_selection_totals, toggle_asset_group, toggle_probable_asset
        asset_plan = {
            "plan_id": "plan-123",
            "totals": {"here": 2, "available": 3, "probable": 1, "unavailable": 1},
            "groups": [
                {
                    "key": "roms:snes",
                    "kind": "roms",
                    "system": "snes",
                    "count": 2,
                    "bytes": 300,
                    "items": [
                        {"key": "r0", "kind": "roms", "system": "snes", "path": "Zelda.sfc", "size": 200, "action": "download"},
                        {"key": "r1", "kind": "roms", "system": "snes", "path": "Mario.sfc", "size": 100, "action": "download"},
                    ],
                },
                {
                    "key": "music:",
                    "kind": "music",
                    "system": "",
                    "count": 1,
                    "bytes": 50,
                    "items": [
                        {"key": "m0", "kind": "music", "system": "", "path": "Song.mp3", "size": 50, "action": "download"},
                    ],
                },
            ],
            "probable": [
                {"key": "r2", "kind": "roms", "system": "snes", "path": "Metroid.sfc", "size": 400, "action": "download"},
            ],
            "unavailable": [
                {"key": "r3", "kind": "roms", "system": "snes", "path": "Kirby.sfc", "size": 500, "action": ""},
            ],
            "sources": [{"drone_id": "peer-1", "name": "Den", "status": "ok", "matched": 4}],
            "space": {"roms": {"path": "/userdata/roms", "free": 500, "group": "dev1"}},
        }
        client = _FakeApiClient(
            get_responses={"/admin/backups": {"backups": [], "snapshots": []}},
            post_responses={
                f"/admin/backups/{_BACKUP_ID}/plan": {"mode": "overlay", "totals": {"new": 1}, "warnings": []},
                f"/admin/backups/{_BACKUP_ID}/assets/plan": asset_plan,
                f"/admin/backups/{_BACKUP_ID}/restore": {"state": "running", "assets": {"state": "pending"}},
            },
        )
        screen = BackupsScreen(client)
        screen._open_restore(self._local())
        # BIOS is off by default; ROMs, movies and music are on by default.
        self.assertEqual(screen.dialog["asset_kinds"], {"roms", "movies", "music"})
        screen._plan_assets()
        self.assertEqual(
            client.post_calls[0],
            (f"/admin/backups/{_BACKUP_ID}/assets/plan", {"kinds": ["roms", "movies", "music"], "include_bios": False}),
        )
        self.assertTrue(screen.dialog["fetch_assets"])
        # Exact matches are selected by default; probable matches are unticked by default.
        self.assertEqual(asset_selection_totals(screen.dialog), (3, 350))
        self.assertEqual(_asset_space_warnings(screen.dialog, asset_plan), [])
        # Tick a probable match -> exceeds free space (300 + 400 = 700 > 500) -> warns, never refuses.
        toggle_probable_asset(screen.dialog, "r2", True)
        self.assertEqual(asset_selection_totals(screen.dialog), (4, 750))
        warnings = _asset_space_warnings(screen.dialog, asset_plan)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Low free space", warnings[0])
        # Untick the music group.
        toggle_asset_group(screen.dialog, asset_plan["groups"][1], False)
        self.assertEqual(asset_selection_totals(screen.dialog), (3, 700))
        screen._preview()
        screen._restore()
        restore_call = client.post_calls[-1]
        self.assertEqual(restore_call[0], f"/admin/backups/{_BACKUP_ID}/restore")
        self.assertEqual(
            restore_call[1]["assets"],
            {
                "plan_id": "plan-123",
                "selected": ["r2"],
                "deselected": [],
                "deselected_groups": ["music:"],
            },
        )

    def test_get_missing_games_standalone_and_batch_cancel(self) -> None:
        asset_plan = {
            "plan_id": "plan-standalone",
            "totals": {"here": 0, "available": 1, "probable": 0, "unavailable": 2},
            "groups": [
                {
                    "key": "roms:snes",
                    "kind": "roms",
                    "system": "snes",
                    "count": 1,
                    "bytes": 200,
                    "items": [
                        {"key": "r0", "kind": "roms", "system": "snes", "path": "Zelda.sfc", "size": 200, "action": "download"},
                    ],
                },
            ],
            "probable": [],
            "unavailable": [{"key": "r1", "kind": "roms", "system": "snes", "path": "Gone.sfc", "size": 100, "action": ""}],
            "sources": [{"drone_id": "peer-1", "name": "Den", "status": "ok", "matched": 1}],
            "space": {},
        }
        client = _FakeApiClient(
            get_responses={"/admin/backups": {"backups": [], "snapshots": []}},
            post_responses={
                f"/admin/backups/{_BACKUP_ID}/assets/plan": asset_plan,
                f"/admin/backups/{_BACKUP_ID}/assets/fetch": {"batch_id": "batch-99", "queued": 1, "unavailable": 2},
                "/admin/downloads/batches/batch-99/cancel": {"status": "cancelled", "cancelled": 1},
            },
        )
        screen = BackupsScreen(client)
        screen._open_missing_games(self._local())
        screen.assets_dialog["asset_kinds"].add("bios")
        screen._plan_missing_games()
        self.assertEqual(
            client.post_calls[0],
            (f"/admin/backups/{_BACKUP_ID}/assets/plan", {"kinds": ["roms", "movies", "music", "bios"], "include_bios": True}),
        )
        screen._fetch_missing_games()
        self.assertIsNone(screen.assets_dialog)
        self.assertEqual(screen.active_batch["batch_id"], "batch-99")
        self.assertIn("2 unavailable", screen.message)
        screen._cancel_batch("batch-99")
        self.assertIsNone(screen.active_batch)
        self.assertEqual(client.post_calls[-1][0], "/admin/downloads/batches/batch-99/cancel")


class AboutScreenTests(unittest.TestCase):
    def test_reload_url_uses_the_self_row_reachable_url(self) -> None:
        client = _FakeApiClient(get_responses={"/admin/swarm/overview": {
            "drones": [
                {"drone_id": "peer-1", "is_self": False, "reachable_url": "https://peer-1.local"},
                {"drone_id": "self", "is_self": True, "reachable_url": "https://batocera.local"},
            ]
        }})
        screen = AboutScreen(client)
        screen._reload_url()
        self.assertEqual(screen.web_url, "https://batocera.local")
        self.assertIsNone(screen.error)

    def test_reload_url_surfaces_error(self) -> None:
        client = _FakeApiClient(get_error=DroneApiError("could not reach Drone"))
        screen = AboutScreen(client)
        screen._reload_url()
        self.assertEqual(screen.error, "could not reach Drone")
        self.assertEqual(screen.web_url, "")

    def test_reload_url_handles_missing_self_row(self) -> None:
        client = _FakeApiClient(get_responses={"/admin/swarm/overview": {"drones": []}})
        screen = AboutScreen(client)
        screen._reload_url()
        self.assertEqual(screen.web_url, "")
        self.assertIsNone(screen.error)


class AppShellTests(unittest.TestCase):
    def test_starts_on_about_and_enters_it_lazily(self) -> None:
        client = _FakeApiClient(get_responses={"/admin/swarm/overview": {"active": False, "drones": []}})
        shell = AppShell(client, "batocera", on_quit=lambda: None)
        self.assertEqual(shell.section, "about")
        shell.on_enter()
        self.assertIn("about", shell._entered_keys)
        self.assertNotIn("swarm", shell._entered_keys)
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
            "/admin/backups": {"backups": [], "snapshots": []},
            "/admin/backups/swarm": {"sources": []},
        })
        shell = AppShell(client, "batocera", on_quit=lambda: None)
        shell.on_enter()  # about, default section
        shell._select_section("swarm")
        shell._select_section("vpn")
        shell._select_section("backups")
        shell._select_section("backups")  # re-selecting shouldn't re-enter
        self.assertEqual(shell._entered_keys, {"about", "swarm", "vpn", "backups"})

    def test_quit_button_logic_calls_callback(self) -> None:
        client = _FakeApiClient()
        calls = []
        shell = AppShell(client, "batocera", on_quit=lambda: calls.append("quit"))
        shell.on_quit()
        self.assertEqual(calls, ["quit"])

    def test_queued_section_load_exposes_loading_before_network_call(self) -> None:
        client = _FakeApiClient(get_responses={
            "/admin/swarm/overview": {"active": False, "drones": []},
        })
        shell = AppShell(client, "batocera", on_quit=lambda: None)
        shell.on_enter()
        calls_before = list(client.get_calls)
        shell._queue_section("swarm", "Swarm")
        self.assertEqual(client.get_calls, calls_before)
        self.assertEqual(shell.deferred_action_label, "Loading Swarm...")
        shell.wait_for_deferred_action()
        overview_calls = [call for call in client.get_calls[len(calls_before):] if "/admin/swarm/overview" in call]
        self.assertEqual(overview_calls, ["/admin/swarm/overview?probe=0"])
        self.assertIsNone(shell.deferred_action_label)


class BackgroundActionTests(unittest.TestCase):
    def test_running_action_does_not_block_render_thread(self) -> None:
        started = threading.Event()
        release = threading.Event()
        screen = Screen()

        def slow_action() -> None:
            started.set()
            release.wait(2)

        screen.defer_action("Loading...", slow_action)
        screen.run_deferred_action()
        self.assertTrue(started.wait(1))
        self.assertEqual(screen.deferred_action_label, "Loading...")
        self.assertIsNotNone(screen._deferred_action.thread)
        self.assertTrue(screen._deferred_action.thread.is_alive())
        release.set()
        screen.wait_for_deferred_action()
        self.assertIsNone(screen.deferred_action_label)


class ItemMetadataLabelTests(unittest.TestCase):
    """Paired peers supply episode metadata, so malformed values must not raise."""

    def test_numeric_and_digit_string_episodes_are_formatted(self):
        self.assertEqual(
            _item_metadata_label("movies", {"show_title": "Show", "season": 2, "episode": "5"}),
            "Show S02E05",
        )

    def test_hostile_season_or_episode_values_fall_back_without_raising(self):
        for season, episode in (
            ("not-a-number", 1),
            (1, "x"),
            (float("nan"), 1),
            (float("inf"), 1),
            (True, 1),
            (10**40, 1),
            (-1, 1),
            ([1], {"a": 1}),
            ("1" * 5000, 1),
            ("\u0661", 1),
        ):
            with self.subTest(season=season, episode=episode):
                label = _item_metadata_label(
                    "movies", {"show_title": "Show", "season": season, "episode": episode}
                )
                self.assertTrue(label.startswith("Show"))
                self.assertNotIn("not-a-number", label)

    def test_season_only_shows_season_label(self):
        self.assertEqual(_item_metadata_label("movies", {"show_title": "Show", "season": 3}), "Show Season 3")


class DownloadPollThreadTests(unittest.TestCase):
    def test_unexpected_poll_error_is_captured_not_raised(self):
        class _Boom(_FakeApiClient):
            def get(self, *_args, **_kwargs):
                raise RuntimeError("boom")

        screen = SwarmScreen(_Boom())
        screen._fetch_download_snapshot()  # must not raise
        self.assertIsNone(screen._download_poll_result)
        self.assertIn("RuntimeError", screen._download_poll_error)


class SearchDrawKeyTests(unittest.TestCase):
    def _draw_search(self, screen, item, *, pressed_key=None, popup_open=False, patch_details=True):
        from contextlib import ExitStack
        from unittest import mock

        from ui.screens import swarm as swarm_module

        imgui = swarm_module.imgui
        items = [item] if item is not None else []
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(screen, "_visible_browse_items", return_value=items))
            stack.enter_context(mock.patch.object(screen, "_draw_search_result_row"))
            if patch_details:
                stack.enter_context(mock.patch.object(screen, "_draw_details_popup"))
            stack.enter_context(mock.patch.object(swarm_module, "search_box", return_value=("", False)))
            stack.enter_context(mock.patch.object(swarm_module, "tab_button"))
            stack.enter_context(mock.patch.object(imgui, "checkbox", return_value=(False, False)))
            stack.enter_context(mock.patch.object(imgui, "begin_combo", return_value=False))
            stack.enter_context(mock.patch.object(imgui, "button", return_value=False))
            stack.enter_context(mock.patch.object(imgui, "get_scroll_max_y", return_value=0.0))
            stack.enter_context(mock.patch.object(imgui, "is_popup_open", return_value=popup_open))
            stack.enter_context(mock.patch.object(imgui, "begin_popup_modal", return_value=(True, True)))
            stack.enter_context(
                mock.patch.object(imgui, "is_key_pressed", side_effect=lambda key, *a: key == pressed_key)
            )
            screen._draw_search()

    def test_draw_search_with_focused_row_opens_details_on_y(self) -> None:
        from ui.screens import swarm as swarm_module

        screen = SwarmScreen(_FakeApiClient())
        item = {"title": "Sonic", "drone_id": "peer1", "drone_name": "Arcade"}
        screen.focused_row_index = 0
        self._draw_search(screen, item, pressed_key=swarm_module.imgui.Key.gamepad_face_up)
        self.assertIs(screen.details_item, item)
        self.assertTrue(screen._details_popup_just_opened)

    def test_draw_search_opens_and_closes_details_modal_with_b(self) -> None:
        from unittest import mock

        from ui.screens import swarm as swarm_module

        screen = SwarmScreen(_FakeApiClient())
        item = {"title": "Sonic", "drone_id": "peer1", "drone_name": "Arcade"}
        screen.focused_row_index = 0
        imgui = swarm_module.imgui
        with mock.patch.object(imgui, "open_popup") as open_popup:
            self._draw_search(
                screen,
                item,
                pressed_key=imgui.Key.gamepad_face_up,
                patch_details=False,
            )
        open_popup.assert_called_once_with("Asset Details")
        self.assertIs(screen.details_item, item)

        with mock.patch.object(imgui, "close_current_popup") as close_popup:
            self._draw_search(
                screen,
                item,
                pressed_key=imgui.Key.gamepad_face_right,
                popup_open=True,
                patch_details=False,
            )
        close_popup.assert_called_once_with()
        self.assertIsNone(screen.details_item)

    def test_draw_search_with_focused_row_downloads_on_x(self) -> None:
        from unittest import mock

        from ui.screens import swarm as swarm_module

        screen = SwarmScreen(_FakeApiClient())
        item = {"title": "Sonic", "drone_id": "peer1", "drone_name": "Arcade"}
        screen.focused_row_index = 0
        with mock.patch.object(screen, "_download_item") as download_item:
            self._draw_search(screen, item, pressed_key=swarm_module.imgui.Key.gamepad_face_left)
        download_item.assert_called_once_with(item)

    def test_draw_search_ignores_x_and_y_when_popup_is_open(self) -> None:
        from unittest import mock

        from ui.screens import swarm as swarm_module

        screen = SwarmScreen(_FakeApiClient())
        item = {"title": "Sonic", "drone_id": "peer1", "drone_name": "Arcade"}
        screen.focused_row_index = 0
        with mock.patch.object(screen, "_download_item") as download_item:
            self._draw_search(
                screen,
                item,
                pressed_key=swarm_module.imgui.Key.gamepad_face_left,
                popup_open=True,
            )
            self._draw_search(
                screen,
                item,
                pressed_key=swarm_module.imgui.Key.gamepad_face_up,
                popup_open=True,
            )
        download_item.assert_not_called()
        self.assertIsNone(screen.details_item)

    def test_draw_search_ignores_x_and_y_when_deferred_action_active(self) -> None:
        from unittest import mock

        from ui.screens import swarm as swarm_module

        screen = SwarmScreen(_FakeApiClient())
        screen.defer_action("Working...", lambda: None)
        item = {"title": "Sonic", "drone_id": "peer1", "drone_name": "Arcade"}
        screen.focused_row_index = 0
        with mock.patch.object(screen, "_download_item") as download_item:
            self._draw_search(screen, item, pressed_key=swarm_module.imgui.Key.gamepad_face_left)
            self._draw_search(screen, item, pressed_key=swarm_module.imgui.Key.gamepad_face_up)
        download_item.assert_not_called()
        self.assertIsNone(screen.details_item)

    def test_draw_search_ignores_x_and_y_when_focus_out_of_bounds(self) -> None:
        from unittest import mock

        from ui.screens import swarm as swarm_module

        screen = SwarmScreen(_FakeApiClient())
        item = {"title": "Sonic", "drone_id": "peer1", "drone_name": "Arcade"}
        screen.focused_row_index = 5
        with mock.patch.object(screen, "_download_item") as download_item:
            self._draw_search(screen, item, pressed_key=swarm_module.imgui.Key.gamepad_face_up)
            self._draw_search(screen, None, pressed_key=swarm_module.imgui.Key.gamepad_face_left)
        download_item.assert_not_called()
        self.assertIsNone(screen.details_item)


if __name__ == "__main__":
    unittest.main()
