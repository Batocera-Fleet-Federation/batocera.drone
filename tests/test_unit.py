import base64
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from app.mock_data import seed_mock_userdata
from app.rom_api import BasicAuth, LaunchBoxClient, RomRepository, _clean_rom_title, _launchbox_platform_for_system


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

    def test_search_roms_from_mock_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            seed_mock_userdata(root)
            repo = RomRepository(root / "roms", root / "bios")
            results = repo.search_roms("mario")
            self.assertTrue(any(item["name"].lower().startswith("mario") for item in results))

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


class LaunchBoxMappingTests(unittest.TestCase):
    def test_launchbox_title_cleanup_replaces_special_separators(self) -> None:
        self.assertEqual(_clean_rom_title("Mega Man: The-Wily;Wars [USA] <Rev 1>.zip"), "Mega Man The Wily Wars USA Rev 1")

    def test_batocera_system_maps_to_launchbox_platform_name(self) -> None:
        self.assertEqual(_launchbox_platform_for_system("ps2"), "Sony Playstation 2")
        self.assertEqual(_launchbox_platform_for_system("snes"), "Super Nintendo Entertainment System")

    def test_launchbox_search_supplies_platform_filter(self) -> None:
        urls = []

        class FakeLaunchBoxClient(LaunchBoxClient):
            def _get_json(self, url: str) -> dict:
                urls.append(url)
                return {"data": []}

        FakeLaunchBoxClient().search("Chrono Trigger", system="ps2")
        self.assertTrue(urls)
        self.assertIn("platform=Sony%20Playstation%202", urls[0])


if __name__ == "__main__":
    unittest.main()
