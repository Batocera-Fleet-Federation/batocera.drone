import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app.mock_data import seed_mock_userdata
from app.rom_api import Settings, create_server


def _auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


class MockServerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name) / "userdata"
        seed_mock_userdata(self._root)

        self._old_env = dict(os.environ)
        os.environ["USERDATA_ROOT"] = str(self._root)
        os.environ["ROMS_ROOT"] = str(self._root / "roms")
        os.environ["BIOS_ROOT"] = str(self._root / "bios")
        os.environ["THEMES_ROOT"] = str(self._root / "themes")
        os.environ["BATOCERA_CONF_FILE"] = str(self._root / "system" / "batocera.conf")
        os.environ["ES_SETTINGS_FILE"] = str(
            self._root / "system" / "configs" / "emulationstation" / "es_settings.cfg"
        )
        os.environ["ROM_API_USERNAME"] = "admin"
        os.environ["ROM_API_PASSWORD"] = "changeme"
        os.environ["HTTPS_PORT"] = "0"
        os.environ["HTTP_ONLY"] = "1"
        os.environ["LOG_DIR"] = str(Path(self._tmp.name) / "logs")
        os.environ["ALLOW_CONTENT_DOWNLOAD"] = "true"

        self.settings = Settings.from_env()
        try:
            self.server = create_server(self.settings)
        except PermissionError as error:
            self.skipTest(f"Socket bind is not allowed in this environment: {error}")
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        os.environ.clear()
        os.environ.update(self._old_env)
        self._tmp.cleanup()

    def _get_json(self, path: str) -> dict:
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", _auth_header("admin", "changeme"))
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)

    def test_systems_endpoint(self) -> None:
        payload = self._get_json("/v1/api/systems")
        names = {item["name"] for item in payload["systems"]}
        self.assertIn("snes", names)

    def test_admin_logs_endpoint(self) -> None:
        payload = self._get_json("/v1/api/admin/logs/es_launch_stdout?lines=20")
        self.assertEqual(payload["source"], "es_launch_stdout")
        self.assertTrue(any("launch emulator" in line for line in payload["content"]))

    def test_admin_configs_endpoint(self) -> None:
        payload = self._get_json("/v1/api/admin/configs/retroarch?max_bytes=65536")
        self.assertEqual(payload["source"], "retroarch")
        self.assertEqual(payload["type"], "file")
        self.assertTrue(any("menu_driver" in line for line in payload["content"]))


if __name__ == "__main__":
    unittest.main()
