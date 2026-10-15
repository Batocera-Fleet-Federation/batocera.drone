"""Issue #152 HTTP boundaries: malformed bodies, non-finite numbers, and unknown bindings.

The Media Keys routes accept administrator JSON that ends up deciding whether a
key can reboot the machine. Every malformed request must be refused with a
structured 4xx (never a 500, never a stored binding that weakens the hold).
"""

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

from app.transfer.peer_workers import stop_local_network_workers


class MalformedHttpInputTests(unittest.TestCase):
    """HTTP-level: hostile bodies get structured 4xx errors and never change state."""

    def setUp(self):
        from app.drone_api import Settings, create_server
        from app.mock_data import seed_mock_userdata

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        root = base / "userdata"
        seed_mock_userdata(root)
        install_root = base / "drone-app"
        for target in ("app.integrations.media_keys.paths.drone_install_root",
                       "app.integrations.streamdeck.paths.drone_install_root"):
            patcher = mock.patch(target, return_value=install_root)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"), "THEMES_ROOT": str(root / "themes"),
            "BATOCERA_CONF_FILE": str(root / "system" / "batocera.conf"),
            "ES_SETTINGS_FILE": str(root / "system" / "configs" / "emulationstation" / "es_settings.cfg"),
            "DRONE_APP_USERNAME": "admin", "DRONE_APP_PASSWORD": "changeme", "HTTPS_PORT": "0", "HTTP_ONLY": "1",
            "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1", "LOG_DIR": str(base / "logs"), "ROM_METADATA_POLL_SECONDS": "0",
            "DRONE_STATE_DATABASE_FILE": str(base / "state.sqlite3"),
            "DRONE_DEVICE_ID": "media-keys-malformed-uat",
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
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=3)
            stop_local_network_workers()
        self.addCleanup(stop)

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/api/auth/login", method="POST",
            data=json.dumps({"username": "admin", "password": "changeme"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            jar = SimpleCookie()
            jar.load(response.headers.get("Set-Cookie"))
        morsel = next(iter(jar.values()))
        self.cookie = f"{morsel.key}={morsel.value}"

    def post_raw(self, path, raw, content_type="application/json"):
        url = f"http://127.0.0.1:{self.port}/v1/api/admin/integrations/media-keys{path}"
        request = urllib.request.Request(url, data=raw, method="POST",
                                         headers={"Cookie": self.cookie, "Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            body = error.read()
            try:
                return error.code, json.loads(body or b"{}")
            except ValueError:
                return error.code, {}

    def status(self):
        url = f"http://127.0.0.1:{self.port}/v1/api/admin/integrations/media-keys/status"
        request = urllib.request.Request(url, headers={"Cookie": self.cookie})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def test_non_finite_and_negative_hold_seconds_are_rejected_without_changing_the_binding(self):
        before = next(row for row in self.status()["bindings"] if row["id"] == "reboot")
        for literal in ("NaN", "Infinity", "-Infinity", "-1", "99"):
            raw = ('{"action_type": "builtin", "action_id": "reboot", "hold_seconds": %s}' % literal).encode()
            code, body = self.post_raw("/bindings/reboot", raw)
            self.assertEqual(code, 400, (literal, code, body))
        after = next(row for row in self.status()["bindings"] if row["id"] == "reboot")
        self.assertEqual(before["hold_seconds"], after["hold_seconds"])
        self.assertTrue(after["requires_hold"])

    def test_non_object_bodies_are_client_errors_not_server_errors(self):
        for path in ("/bindings/exit", "/bindings/reboot/test", "/scripts", "/enable"):
            for raw in (b"[1, 2]", b'"reboot"', b"7", b"null"):
                with self.subTest(path=path, raw=raw):
                    code, _body = self.post_raw(path, raw)
                    self.assertLess(code, 500, f"{path} {raw!r} -> {code}")

    def test_malformed_json_is_a_client_error(self):
        for path in ("/bindings/exit", "/bindings/reboot/test"):
            code, _body = self.post_raw(path, b"{not json")
            self.assertTrue(400 <= code < 500, (path, code))

    def test_unknown_binding_ids_are_refused(self):
        for binding in ("KEY_POWER", "..%2F..%2Fetc", "reboot%00", "REBOOT"):
            code, _body = self.post_raw(f"/bindings/{binding}", b'{"action_type": "none"}')
            self.assertTrue(400 <= code < 500, (binding, code))
            code, _body = self.post_raw(f"/bindings/{binding}/test", b"{}")
            self.assertTrue(400 <= code < 500, (binding, code))

    def test_string_confirm_flag_does_not_count_as_confirmation(self):
        for flag in ('"true"', "1", '"yes"', "null", "[true]"):
            code, body = self.post_raw("/bindings/reboot/test", ('{"confirm": %s}' % flag).encode())
            self.assertEqual(code, 400, (flag, code, body))
            self.assertNotIn(body.get("status"), ("ok", "holding"))


if __name__ == "__main__":
    unittest.main()
