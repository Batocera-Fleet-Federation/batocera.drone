import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from client.gamelist_integration import (
    IMAGE_RELATIVE_PATH,
    MARQUEE_RELATIVE_PATH,
    PORTS_VIDEO_MODE_KEY,
    PORTS_VIDEO_MODE_VALUE,
    THUMBNAIL_RELATIVE_PATH,
    ensure_ports_gamelist,
)


class PortsGamelistIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.userdata = Path(self._tmp.name) / "userdata"
        self.ports = self.userdata / "roms" / "ports"
        self.batocera_conf = self.userdata / "system" / "batocera.conf"
        (self.ports / "images").mkdir(parents=True)
        self.batocera_conf.parent.mkdir(parents=True)
        self.batocera_conf.write_text("global.videomode=max-1920x1080\n", encoding="utf-8")
        (self.ports / "batocera-drone-client.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.ports / "images" / "batocera-drone_marquee.png").write_bytes(b"png")
        (self.ports / "images" / "main.jpg").write_bytes(b"jpg")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _entry(self, path: str):
        root = ET.parse(self.ports / "gamelist.xml").getroot()
        return next(game for game in root.findall("game") if game.findtext("path") == path)

    def _ensure(self):
        return ensure_ports_gamelist(self.ports, self.batocera_conf)

    def test_creates_drone_entry_with_artwork_for_a_new_gamelist(self) -> None:
        result = self._ensure()

        self.assertEqual(result["status"], "updated")
        entry = self._entry("./batocera-drone-client.sh")
        self.assertEqual(entry.findtext("name"), "Batocera Drone")
        self.assertEqual(entry.findtext("marquee"), MARQUEE_RELATIVE_PATH)
        self.assertEqual(entry.findtext("image"), IMAGE_RELATIVE_PATH)
        self.assertEqual(entry.findtext("thumbnail"), THUMBNAIL_RELATIVE_PATH)
        self.assertEqual(result["launcher_config"]["status"], "updated")
        self.assertIn(
            f"{PORTS_VIDEO_MODE_KEY}={PORTS_VIDEO_MODE_VALUE}",
            self.batocera_conf.read_text(encoding="utf-8"),
        )

    def test_updates_only_drone_artwork_and_preserves_other_ports_metadata(self) -> None:
        (self.ports / "gamelist.xml").write_text(
            "<gameList>"
            "<game><path>./other.sh</path><name>Other Port</name>"
            "<image>./images/other.jpg</image><thumbnail>./images/other-thumb.jpg</thumbnail>"
            "<marquee>./images/other.png</marquee></game>"
            "<game><path>./batocera-drone-client.sh</path><name>My Drone Name</name>"
            "<favorite>true</favorite><image>./images/old.jpg</image>"
            "<thumbnail>./images/old-thumb.jpg</thumbnail>"
            "<marquee>./images/old.png</marquee></game>"
            "</gameList>",
            encoding="utf-8",
        )

        result = self._ensure()

        self.assertEqual(result["status"], "updated")
        other = self._entry("./other.sh")
        drone = self._entry("./batocera-drone-client.sh")
        self.assertEqual(other.findtext("image"), "./images/other.jpg")
        self.assertEqual(other.findtext("thumbnail"), "./images/other-thumb.jpg")
        self.assertEqual(other.findtext("marquee"), "./images/other.png")
        self.assertEqual(drone.findtext("name"), "My Drone Name")
        self.assertEqual(drone.findtext("favorite"), "true")
        self.assertEqual(drone.findtext("image"), IMAGE_RELATIVE_PATH)
        self.assertEqual(drone.findtext("thumbnail"), THUMBNAIL_RELATIVE_PATH)
        self.assertEqual(drone.findtext("marquee"), MARQUEE_RELATIVE_PATH)

    def test_second_run_is_idempotent(self) -> None:
        self._ensure()
        before = (self.ports / "gamelist.xml").read_bytes()

        result = self._ensure()

        self.assertEqual(result["status"], "current")
        self.assertEqual(result["launcher_config"]["status"], "current")
        self.assertEqual((self.ports / "gamelist.xml").read_bytes(), before)

    def test_preserves_an_explicit_user_video_mode(self) -> None:
        original = (
            "global.videomode=max-1920x1080\n"
            f"{PORTS_VIDEO_MODE_KEY}=1920x1080.60.00\n"
        )
        self.batocera_conf.write_text(original, encoding="utf-8")

        result = self._ensure()

        self.assertEqual(result["launcher_config"]["status"], "current")
        self.assertTrue(result["launcher_config"]["preserved_existing"])
        self.assertEqual(result["launcher_config"]["value"], "1920x1080.60.00")
        self.assertEqual(self.batocera_conf.read_text(encoding="utf-8"), original)

    def test_refuses_to_write_metadata_when_marquee_is_missing(self) -> None:
        (self.ports / "images" / "batocera-drone_marquee.png").unlink()

        with self.assertRaises(FileNotFoundError):
            self._ensure()
        self.assertFalse((self.ports / "gamelist.xml").exists())

    def test_refuses_to_write_metadata_when_image_is_missing(self) -> None:
        (self.ports / "images" / "main.jpg").unlink()

        with self.assertRaises(FileNotFoundError):
            self._ensure()
        self.assertFalse((self.ports / "gamelist.xml").exists())

    def test_malformed_existing_gamelist_is_never_replaced(self) -> None:
        gamelist = self.ports / "gamelist.xml"
        original = b"<gameList><game>broken"
        gamelist.write_bytes(original)

        with self.assertRaises(ET.ParseError):
            self._ensure()
        self.assertEqual(gamelist.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
