import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app.mock_data import seed_mock_userdata
from app.drone_api import Settings, create_server


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
        os.environ["DRONE_APP_USERNAME"] = "admin"
        os.environ["DRONE_APP_PASSWORD"] = "changeme"
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

    def _get_bytes(self, path: str) -> bytes:
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", _auth_header("admin", "changeme"))
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read()

    def _post_json(self, path: str, payload: dict) -> dict:
        url = f"http://127.0.0.1:{self.port}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", _auth_header("admin", "changeme"))
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            response_body = resp.read().decode("utf-8")
        return json.loads(response_body)

    def test_systems_endpoint(self) -> None:
        payload = self._get_json("/v1/api/systems")
        names = {item["name"] for item in payload["systems"]}
        self.assertIn("snes", names)

    def test_rom_download_by_unique_id(self) -> None:
        payload = self._get_json("/v1/api/systems/snes")
        rom = next(item for item in payload["roms"] if item["rom_file"] == "Chrono Trigger (USA).zip")
        data = self._get_bytes(f"/v1/api/systems/snes/roms/{rom['unique_id']}")
        self.assertEqual(data, b"FAKE-SNES-ROM-1")

    def test_api_admin_status_and_openapi_mtls_guidance(self) -> None:
        payload = self._get_json("/v1/api/admin/api/status")
        self.assertIn("/v1/api/swagger", payload["swagger_url"])
        self.assertIn("certificate", payload)
        self.assertNotIn("private_key", json.dumps(payload).lower())

        spec = self._get_json("/v1/api/openapi.json")
        self.assertIn("mtls", json.dumps(spec).lower())

    def test_overmind_integration_uses_authorization_token_label(self) -> None:
        js = self._get_bytes("/static/js/drone.js")
        self.assertIn(b"Authorization Token", js)
        self.assertIn(b"Claim Ownership", js)
        self.assertNotIn(b"Integration Password", js)

    def test_content_mascot_is_served(self) -> None:
        image = self._get_bytes("/content/batocera-swarm-mascot.jpg")
        self.assertTrue(image.startswith(b"\xff\xd8\xff"))

    def test_admin_logs_endpoint(self) -> None:
        payload = self._get_json("/v1/api/admin/logs/es_launch_stdout?lines=20")
        self.assertEqual(payload["source"], "es_launch_stdout")
        self.assertTrue(any("launch emulator" in line for line in payload["content"]))

    def test_admin_configs_endpoint(self) -> None:
        payload = self._get_json("/v1/api/admin/configs/retroarch?max_bytes=65536")
        self.assertEqual(payload["source"], "retroarch")
        self.assertEqual(payload["type"], "file")
        self.assertTrue(any("menu_driver" in line for line in payload["content"]))

    def test_admin_missing_artwork_endpoint(self) -> None:
        missing_rom = self._root / "roms" / "snes" / "Missing Game (USA).zip"
        gamelist = self._root / "roms" / "snes" / "gamelist.xml"
        text = gamelist.read_text(encoding="utf-8").replace(
            "</gameList>",
            "<game><path>./Missing Game (USA).zip</path><name>Missing Game</name></game></gameList>",
        )
        gamelist.write_text(text, encoding="utf-8")
        if missing_rom.exists():
            missing_rom.unlink()

        payload = self._get_json("/v1/api/admin/artwork/missing?limit=2&offset=0&fields=image,marquee&systems=snes")
        self.assertGreater(payload["count"], 0)
        self.assertLessEqual(payload["returned"], 2)
        self.assertEqual(payload["limit"], 2)
        self.assertEqual(payload["offset"], 0)
        self.assertIn("snes", payload["systems"])
        self.assertEqual(payload["systems_filtered"], ["snes"])
        self.assertEqual(payload["selected_fields"], ["image", "marquee"])
        self.assertTrue(any("image" in item["missing"] for item in payload["roms"]))
        self.assertIn("rom_exists", payload["roms"][0])
        self.assertTrue(payload["roms"][0]["rom_exists"])

        filtered = self._get_json("/v1/api/admin/artwork/missing?limit=2&offset=0&fields=any&q=castlevania")
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["query"], "castlevania")
        self.assertEqual(filtered["roms"][0]["system"], "psx")

        missing = self._get_json("/v1/api/admin/artwork/missing?fields=any&systems=snes&rom_status=missing&refresh=1")
        self.assertEqual(missing["rom_status"], "missing")
        self.assertTrue(all(not item["rom_exists"] for item in missing["roms"]))
        self.assertTrue(any(item["title"] == "Missing Game" for item in missing["roms"]))

        existing = self._get_json("/v1/api/admin/artwork/missing?fields=any&systems=snes&rom_status=exists&refresh=1")
        self.assertEqual(existing["rom_status"], "exists")
        self.assertTrue(all(item["rom_exists"] for item in existing["roms"]))

    def test_admin_remove_gamelist_entry_endpoint(self) -> None:
        result = self._post_json(
            "/v1/api/admin/artwork/gamelist/remove",
            {"system": "snes", "rom_path": "Chrono Trigger (USA).zip"},
        )
        self.assertTrue(result["removed"])
        payload = self._get_json("/v1/api/admin/artwork/missing?fields=any&systems=snes&refresh=1")
        titles = {item["title"] for item in payload["roms"]}
        self.assertNotIn("Chrono Trigger", titles)

    def test_admin_update_gamelist_entry_endpoint(self) -> None:
        result = self._post_json(
            "/v1/api/admin/artwork/gamelist/update",
            {
                "system": "snes",
                "rom_path": "Chrono Trigger (USA).zip",
                "fields": {"name": "Chrono Trigger Admin Edit", "desc": "Updated from artwork admin."},
            },
        )
        self.assertEqual(result["title"], "Chrono Trigger Admin Edit")
        self.assertEqual(result["gamelist"]["desc"], "Updated from artwork admin.")
        text = (self._root / "roms" / "snes" / "gamelist.xml").read_text(encoding="utf-8")
        self.assertIn("Chrono Trigger Admin Edit", text)
        self.assertIn("Updated from artwork admin.", text)

    def test_admin_remove_missing_gamelist_entries_endpoint(self) -> None:
        gamelist = self._root / "roms" / "snes" / "gamelist.xml"
        gamelist.write_text(
            gamelist.read_text(encoding="utf-8").replace(
                "</gameList>",
                "<game><path>./Missing Bulk Game.zip</path><name>Missing Bulk Game</name></game></gameList>",
            ),
            encoding="utf-8",
        )
        result = self._post_json(
            "/v1/api/admin/artwork/gamelist/remove-missing",
            {
                "confirm": "DELETE_MISSING_GAMELIST_ENTRIES",
                "fields": ["any"],
                "systems": ["snes"],
                "q": "Missing Bulk",
            },
        )
        self.assertEqual(result["removed_count"], 1)
        self.assertNotIn("Missing Bulk Game", gamelist.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
