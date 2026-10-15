"""Adversarial UAT for Issue #152 hold-to-confirm: separate presses, real timing, and the HTTP hold contract.

Issue #152 requires that reboot/shutdown need a sustained hold (a single press
does nothing) and that the admin hold-to-test control works. Expected behaviour
is derived from those requirements, not from the current implementation.
Clocks are injected, the built-in executor is a fake, and the HTTP server uses
an isolated temporary install root, so nothing reboots and no real path is touched.
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

from app.integrations.media_keys import dispatch as dispatch_module
from app.integrations.media_keys.config import ConfigStore
from app.integrations.media_keys.dispatch import handle_press
from app.integrations.media_keys.paths import MediaKeysPaths
from app.transfer.peer_workers import stop_local_network_workers


class RecordingControl:
    def __init__(self):
        self.calls = []

    def run_builtin(self, action_id):
        self.calls.append(action_id)
        return {"status": "ok"}


class SeparatePressTests(unittest.TestCase):
    """Dispatcher-level: each physical press is an event with no key-up signal."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = MediaKeysPaths(Path(tmp.name).resolve() / "integrations" / "media-keys")
        self.paths.ensure_layout()
        ConfigStore(self.paths).set_enabled(True)
        self.control = RecordingControl()
        patcher = mock.patch.object(dispatch_module, "_drone_log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_separate_taps_spaced_under_a_second_do_not_add_up_to_a_reboot(self):
        # Eight deliberate taps of the reboot key, released between each, 0.9 s apart
        # (6.3 s in total, longer than the 3 s hold). Each tap is a separate press, so
        # none of them is a hold and the reboot action must never run.
        now = [1000.0]
        statuses = []
        for _ in range(8):
            statuses.append(handle_press("reboot", self.paths, control=self.control,
                                         clock=lambda: now[0])["status"])
            now[0] += 0.9
        self.assertEqual(self.control.calls, [],
                         f"separate taps satisfied the reboot hold; statuses={statuses}")

    def test_sustained_hold_with_kernel_repeat_timing_fires_exactly_once(self):
        # Linux auto-repeat: the first repeat arrives after a ~0.6 s initial delay, then
        # repeats every ~33 ms until release. A real hold of 4 s must run reboot once.
        now = [2000.0]
        repeat_times = [0.0, 0.6] + [0.6 + 0.033 * step for step in range(1, 130)]
        results = []
        for offset in repeat_times:
            now[0] = 2000.0 + offset
            results.append(handle_press("reboot", self.paths, control=self.control,
                                        clock=lambda: now[0])["status"])
        self.assertEqual(self.control.calls, ["reboot"])
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results[0], "holding")


class HoldHttpContractTests(unittest.TestCase):
    """HTTP-level: the admin hold-to-test control posts one request per pulse."""

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
            "DRONE_DEVICE_ID": "media-keys-hold-uat",
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

    def post(self, path, payload):
        url = f"http://127.0.0.1:{self.port}/v1/api/admin/integrations/media-keys{path}"
        request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                         headers={"Cookie": self.cookie, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_holding_pulse_is_progress_not_an_http_error(self):
        # The admin page posts {"confirm": true} while the reboot key is held. The first
        # pulse is only progress: the API answers "holding" and must not return an error
        # status, because the UI's POST helper turns any non-2xx into an error toast and
        # cancels the hold. A single pulse must also never run the reboot.
        status, body = self.post("/bindings/reboot/test", {"confirm": True})
        self.assertEqual(body.get("status"), "holding", body)
        self.assertEqual(status, 200, f"holding pulse returned HTTP {status}: {body}")


if __name__ == "__main__":
    unittest.main()
