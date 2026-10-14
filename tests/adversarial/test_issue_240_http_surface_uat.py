"""Issue #240 adversarial HTTP UAT: admin auth, peer mTLS, dashboard fields.

Expected behaviour, derived from the issue:

* Browser mutations need a real login, JSON, and same-origin. A cookie is not
  a substitute for a paired peer certificate on /peer/multiplayer/*.
* Peer identity is the certificate. A body source_drone_id that disagrees is
  identity_mismatch. Unpaired callers are refused.
* The dashboard payload names peers, sessions, invitations, adapters,
  mechanisms, network, and recent history.
* No endpoint accepts a shell command or raw filesystem path.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class MultiplayerHttpSurfaceUAT(unittest.TestCase):
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
            "DRONE_STATE_DATABASE_FILE": str(base / "state.sqlite3"), "DRONE_DEVICE_ID": "mp-http-adv",
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
        login = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/api/auth/login", method="POST",
            data=json.dumps({"username": "admin", "password": "changeme"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(login, timeout=5) as response:
            jar = SimpleCookie()
            jar.load(response.headers.get("Set-Cookie"))
        morsel = next(iter(jar.values()))
        self.cookie = f"{morsel.key}={morsel.value}"

    def call(self, path, payload=None, *, cookie=True, headers=None, prefix="/admin/multiplayer"):
        url = f"http://127.0.0.1:{self.port}/v1/api{prefix}{path}"
        all_headers = {"Cookie": self.cookie} if cookie else {}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            all_headers["Content-Type"] = "application/json"
        all_headers.update(headers or {})
        request = urllib.request.Request(url, data=data, headers=all_headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            length = error.headers.get("Content-Length")
            body = error.read(int(length)) if length is not None else b""
            error.close()
            return error.code, (json.loads(body) if body[:1] == b"{" else body)

    def test_dashboard_names_the_required_surfaces(self):
        status, overview = self.call("")
        self.assertEqual(status, 200)
        for key in ("enabled", "peers", "sessions", "invitations", "adapters", "mechanisms", "network", "recent", "settings"):
            self.assertIn(key, overview)
        self.assertFalse(overview["settings"]["general"]["auto_accept_invitations"])
        self.assertEqual(overview["settings"]["privacy"]["local_launch_authorization"], "confirm")
        self.assertEqual(overview["network"]["relay"], "none")
        mechanisms = {item["mechanism"] for item in overview["mechanisms"]}
        for name in ("RETROARCH_NETPLAY", "DOLPHIN_NETPLAY", "PPSSPP_ADHOC", "SWITCH_LDN",
                     "EMULATED_LAN", "EMULATOR_NATIVE_NETWORK", "REMOTE_STREAMING"):
            self.assertIn(name, mechanisms)
        tiers = {adapter["id"]: adapter["tier"] for adapter in overview["adapters"]}
        self.assertEqual(tiers["retroarch"], "implemented")
        self.assertEqual(tiers["rpcs3"], "detect_only")

    def test_mutations_need_login_json_and_same_origin(self):
        self.assertEqual(self.call("/settings", {}, cookie=False)[0], 401)
        self.assertEqual(self.call("/settings", {}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.call("/sessions", {"command": "id", "system": "snes", "game_id": "g"})[0] in (400, 404), True)
        self.assertEqual(self.call("/sessions", {"path": "/etc/passwd", "system": "snes", "game_id": "g"})[0] in (400, 404), True)
        self.assertEqual(self.call("/sessions", {"system": "../etc", "game_id": "g"})[0], 400)
        self.assertEqual(self.call("/games?system=../etc")[0], 400)

    def test_peer_routes_require_a_paired_certificate_not_a_browser_cookie(self):
        message = {"message_id": "a" * 16, "issued_at": "2099-01-01T00:00:00+00:00"}
        self.assertEqual(self.call("/invite", message, prefix="/peer/multiplayer", cookie=False)[0], 403)
        self.assertEqual(self.call("/launch", message, prefix="/peer/multiplayer", cookie=True)[0], 403)
        from app.drone_api import RomRequestHandler
        from app.multiplayer.models import new_message_id, now_iso
        invite = {"message_id": new_message_id(), "issued_at": now_iso(), "source_drone_id": "someone-else", "session": {}}
        with mock.patch.object(RomRequestHandler, "_peer_requester_device_id", return_value="peer-x"), \
                mock.patch("app.transfer.local_network.paired_peers", return_value=[{"drone_id": "peer-x", "name": "X"}]):
            status, body = self.call("/invite", invite, prefix="/peer/multiplayer", cookie=False)
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "identity_mismatch")


if __name__ == "__main__":
    unittest.main()
