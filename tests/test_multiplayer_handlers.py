"""Multiplayer HTTP surface: session gate, mutation hardening, structured errors, peer endpoint identity, UI wiring."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from unittest import mock


class MultiplayerHttpTests(unittest.TestCase):
    def setUp(self):
        from app.drone_api import Settings, create_server
        from app.mock_data import seed_mock_userdata
        from app.multiplayer.worker import stop_multiplayer_worker
        from app.transfer.peer_workers import stop_local_network_workers

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        root = base / "userdata"
        seed_mock_userdata(root)
        env = {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"), "THEMES_ROOT": str(root / "themes"),
            "BATOCERA_CONF_FILE": str(root / "system" / "batocera.conf"),
            "ES_SETTINGS_FILE": str(root / "system" / "configs" / "emulationstation" / "es_settings.cfg"),
            "DRONE_APP_USERNAME": "admin", "DRONE_APP_PASSWORD": "changeme", "HTTPS_PORT": "0", "HTTP_ONLY": "1",
            "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1", "LOG_DIR": str(base / "logs"), "ROM_METADATA_POLL_SECONDS": "0",
            "DRONE_STATE_DATABASE_FILE": str(base / "state.sqlite3"), "DRONE_DEVICE_ID": "mp-http-test",
        }
        env_patch = mock.patch.dict(os.environ, env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        try:
            self.server = create_server(Settings.from_env())
        except PermissionError as error:
            self.skipTest(f"Socket bind is not allowed in this environment: {error}")
        self.port = int(self.server.server_address[1])
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            stop_multiplayer_worker()
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=3)
            stop_local_network_workers()
        self.addCleanup(stop)
        login = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/api/auth/login", method="POST",
                                       data=json.dumps({"username": "admin", "password": "changeme"}).encode(),
                                       headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(login, timeout=5) as response:
            jar = SimpleCookie()
            jar.load(response.headers.get("Set-Cookie"))
        morsel = next(iter(jar.values()))
        self.cookie = f"{morsel.key}={morsel.value}"

    def call(self, path, payload=None, *, cookie=True, headers=None, prefix="/admin/multiplayer", raw=None):
        url = f"http://127.0.0.1:{self.port}/v1/api{prefix}{path}"
        all_headers = {"Cookie": self.cookie} if cookie else {}
        data = raw
        if payload is not None:
            data = json.dumps(payload).encode()
            all_headers["Content-Type"] = "application/json"
        all_headers.update(headers or {})
        request = urllib.request.Request(url, data=data, headers=all_headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            # Legacy authentication challenges have no length and may reset the socket
            # with an unread POST body. The status/header is the challenge contract.
            length = error.headers.get("Content-Length")
            body = error.read(int(length)) if length is not None else b""
            error.close()
            return error.code, (json.loads(body) if body[:1] == b"{" else body)

    # ------------------------------------------------------------------ reads
    def test_reads_need_a_session_and_return_the_dashboard(self):
        status, overview = self.call("")
        self.assertEqual(status, 200)
        for key in ("enabled", "peers", "sessions", "invitations", "adapters", "mechanisms", "network", "recent", "settings"):
            self.assertIn(key, overview)
        self.assertEqual({m["mechanism"] for m in overview["mechanisms"]},
                         {"RETROARCH_NETPLAY", "DOLPHIN_NETPLAY", "PPSSPP_ADHOC", "SWITCH_LDN", "EMULATED_LAN", "EMULATOR_NATIVE_NETWORK", "REMOTE_STREAMING"})

    def test_adapter_listing_distinguishes_implemented_from_detection_only(self):
        status, payload = self.call("/adapters")
        self.assertEqual(status, 200)
        tiers = {adapter["id"]: adapter["tier"] for adapter in payload["adapters"]}
        self.assertEqual(tiers["retroarch"], "implemented")
        self.assertEqual(tiers["rpcs3"], "detect_only")
        self.assertEqual(self.call("/adapters/retroarch")[1]["id"], "retroarch")
        self.assertEqual(self.call("/adapters/nope")[0], 404)

    def test_dependency_status_is_readable_and_actions_are_gated(self):
        status, payload = self.call("/dependencies")
        self.assertEqual(status, 200)
        sunshine = payload["dependencies"]["sunshine"]
        for key in ("installed", "running", "can_install", "can_start", "can_uninstall", "job", "web_ui"):
            self.assertIn(key, sunshine)
        # Nothing here may start a real download: only gating and allowlist checks are exercised.
        self.assertEqual(self.call("/dependencies/sunshine/install", {}, cookie=False)[0], 401)
        self.assertEqual(self.call("/dependencies/sunshine/install", {}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.call("/dependencies/sunshine/format-disk", {})[0], 404)
        self.assertEqual(self.call("/dependencies/other/install", {})[0], 404)

    def test_game_browser_requires_a_valid_system_and_filters(self):
        self.assertEqual(self.call("/games")[1]["code"], "invalid_system")
        self.assertEqual(self.call("/games?system=../etc")[0], 400)
        self.assertEqual(self.call("/games?system=snes&status=nope")[1]["code"], "invalid_filter")
        self.assertEqual(self.call("/games?system=snes&limit=100000")[0], 400)
        status, page = self.call("/games?system=snes")
        self.assertEqual(status, 200)
        self.assertIn("games", page)

    def test_unknown_session_and_endpoint(self):
        self.assertEqual(self.call("/sessions/" + "a" * 32)[0], 404)
        self.assertEqual(self.call("/sessions/not-an-id")[1]["code"], "invalid_session_id")
        self.assertEqual(self.call("/nonsense")[0], 404)

    # ----------------------------------------------------------------- mutations
    def test_mutations_need_a_session_json_and_same_origin(self):
        self.assertEqual(self.call("/settings", {}, cookie=False)[0], 401)
        self.assertEqual(self.call("/settings", {}, headers={"Origin": "http://evil.example"})[0], 403)
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/api/admin/multiplayer/settings", data=b"{}",
                                         headers={"Cookie": self.cookie, "Content-Type": "text/plain"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 415)
        self.assertEqual(self.call("/settings", raw=b"{not json", headers={"Content-Type": "application/json"})[0], 400)

    def test_settings_round_trip_and_validation(self):
        status, updated = self.call("/settings", {"general": {"max_simultaneous_sessions": 2}})
        self.assertEqual((status, updated["general"]["max_simultaneous_sessions"]), (200, 2))
        self.assertEqual(self.call("/settings")[1]["general"]["max_simultaneous_sessions"], 2)
        status, error = self.call("/settings", {"general": {"max_simultaneous_sessions": 99}})
        self.assertEqual((status, error["code"]), (400, "invalid_value"))
        self.assertEqual(self.call("/settings", {"privacy": {"auto_launch_anything": True}})[0], 400)

    def test_create_session_rejects_hostile_or_unknown_input(self):
        for body, expected in (({"system": "../x", "game_id": "g"}, 400), ({"system": "snes", "game_id": "g;rm"}, 400),
                               ({"system": "snes", "game_id": "nope"}, 404), ({"system": "snes", "game_id": "g", "mechanism": "SHELL"}, 400),
                               ({"system": "snes", "game_id": "g", "peers": ["a b"]}, 400), ({"game_id": "g"}, 400)):
            with self.subTest(body=body):
                status, error = self.call("/sessions", body)
                self.assertEqual(status, expected)
                self.assertIn("code", error)

    def test_no_endpoint_accepts_commands_or_paths(self):
        for body in ({"command": "id", "system": "snes", "game_id": "g"}, {"path": "/etc/passwd", "system": "snes", "game_id": "g"}):
            self.assertIn(self.call("/sessions", body)[0], (400, 404))

    # --------------------------------------------------------------------- peers
    def test_peer_endpoints_refuse_callers_without_a_paired_certificate(self):
        message = {"message_id": "a" * 16, "issued_at": "2099-01-01T00:00:00+00:00"}
        for path, payload in (("/invite", message), ("/update", message), ("/respond", message), ("/launch", message), ("/match", message)):
            with self.subTest(path=path):
                status, body = self.call(path, payload, prefix="/peer/multiplayer", cookie=False)
                self.assertEqual(status, 403)
        self.assertEqual(self.call("/status", prefix="/peer/multiplayer", cookie=False)[0], 403)
        self.assertEqual(self.call("/compatibility", prefix="/peer/multiplayer", cookie=False)[0], 403)

    def test_peer_identity_comes_from_the_certificate_and_must_be_paired(self):
        from app.drone_api import RomRequestHandler
        message = {"message_id": "a" * 16, "issued_at": "2099-01-01T00:00:00+00:00", "queries": [{"system": "snes", "rom_name": "x.sfc"}]}
        with mock.patch.object(RomRequestHandler, "_peer_requester_device_id", return_value="stranger-drone"):
            status, body = self.call("/match", message, prefix="/peer/multiplayer", cookie=False)
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "not_paired")

    def test_a_paired_peer_can_use_the_peer_endpoints_over_http(self):
        from app.drone_api import RomRequestHandler
        from app.multiplayer.models import new_message_id, now_iso
        paired = [{"drone_id": "peer-x", "name": "Peer X"}]
        message = {"message_id": new_message_id(), "issued_at": now_iso(),
                   "queries": [{"system": "snes", "rom_name": "Nothing.sfc", "size": 1}]}
        with mock.patch.object(RomRequestHandler, "_peer_requester_device_id", return_value="peer-x"), \
                mock.patch("app.transfer.local_network.paired_peers", return_value=paired):
            status, body = self.call("/match", message, prefix="/peer/multiplayer", cookie=False)
            self.assertEqual(status, 200, body)
            self.assertFalse(body["results"][0]["found"])
            self.assertEqual(body["device_id"], "mp-http-test")
            # replaying the identical message is harmless for read-only match, and a bad message is rejected, not crashed on
            self.assertEqual(self.call("/match", message, prefix="/peer/multiplayer", cookie=False)[0], 200)
            status, error = self.call("/respond", {"message_id": new_message_id(), "issued_at": now_iso(), "session_id": "b" * 32, "action": "accept"},
                                      prefix="/peer/multiplayer", cookie=False)
            self.assertEqual((status, error["code"]), (404, "session_not_found"))
            self.assertEqual(self.call("/status", prefix="/peer/multiplayer", cookie=False)[0], 200)
            self.assertEqual(self.call("/nope", {}, prefix="/peer/multiplayer", cookie=False)[0], 404)
            # an invitation from a peer must not appear to come from someone else and must not launch anything
            invite = {"message_id": new_message_id(), "issued_at": now_iso(), "source_drone_id": "someone-else", "session": {}}
            self.assertEqual(self.call("/invite", invite, prefix="/peer/multiplayer", cookie=False)[1]["code"], "identity_mismatch")

    def test_peer_routes_are_not_browser_routes(self):
        # A browser session cookie must not substitute for a peer certificate.
        status, _ = self.call("/respond", {"message_id": "a" * 16}, prefix="/peer/multiplayer", cookie=True)
        self.assertEqual(status, 403)

    # ------------------------------------------------------------------- UI/spec
    def test_ui_wiring_and_theme_contract(self):
        def fetch(path):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
                return response.read().decode("utf-8")
        script = fetch("/static/js/multiplayer.js")
        for needle in ("async function renderMultiplayerPage()", "themed-table", "bff-stack", "data-mp-action", "escapeHtml", "game_running"):
            self.assertIn(needle, script)
        self.assertNotIn("onclick=", script, "no inline handlers: peer/library strings must never reach JavaScript")
        self.assertNotIn("eval(", script)
        router = fetch("/static/js/drone.js")
        self.assertIn('hash === "#multiplayer" || hash.startsWith("#multiplayer/")', router)
        self.assertIn("multiplayerMenuBtn", router)

    def test_openapi_documents_the_multiplayer_surface(self):
        from app.drone_api import OPENAPI_SPEC
        paths = OPENAPI_SPEC["paths"]
        for path in ("/admin/multiplayer", "/admin/multiplayer/sessions", "/admin/multiplayer/sessions/{session_id}/start",
                     "/admin/multiplayer/settings", "/peer/multiplayer/invite", "/peer/multiplayer/respond", "/peer/multiplayer/match"):
            self.assertIn(path, paths)
        self.assertIn({"mutualTLS": []}, paths["/peer/multiplayer/invite"]["post"]["security"])


if __name__ == "__main__":
    unittest.main()
