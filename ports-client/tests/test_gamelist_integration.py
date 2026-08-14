import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from client.gamelist_integration import MARQUEE_RELATIVE_PATH, ensure_ports_gamelist


class PortsGamelistIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ports = Path(self._tmp.name) / "ports"
        (self.ports / "images").mkdir(parents=True)
        (self.ports / "batocera-drone-client.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.ports / "images" / "batocera-drone_marquee.png").write_bytes(b"png")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _entry(self, path: str):
        root = ET.parse(self.ports / "gamelist.xml").getroot()
        return next(game for game in root.findall("game") if game.findtext("path") == path)

    def test_creates_drone_entry_with_marquee_for_a_new_gamelist(self) -> None:
        result = ensure_ports_gamelist(self.ports)

        self.assertEqual(result["status"], "updated")
        entry = self._entry("./batocera-drone-client.sh")
        self.assertEqual(entry.findtext("name"), "Batocera Drone")
        self.assertEqual(entry.findtext("marquee"), MARQUEE_RELATIVE_PATH)

    def test_updates_only_drone_marquee_and_preserves_other_ports_metadata(self) -> None:
        (self.ports / "gamelist.xml").write_text(
            "<gameList>"
            "<game><path>./other.sh</path><name>Other Port</name><marquee>./images/other.png</marquee></game>"
            "<game><path>./batocera-drone-client.sh</path><name>My Drone Name</name>"
            "<favorite>true</favorite><marquee>./images/old.png</marquee></game>"
            "</gameList>",
            encoding="utf-8",
        )

        result = ensure_ports_gamelist(self.ports)

        self.assertEqual(result["status"], "updated")
        other = self._entry("./other.sh")
        drone = self._entry("./batocera-drone-client.sh")
        self.assertEqual(other.findtext("marquee"), "./images/other.png")
        self.assertEqual(drone.findtext("name"), "My Drone Name")
        self.assertEqual(drone.findtext("favorite"), "true")
        self.assertEqual(drone.findtext("marquee"), MARQUEE_RELATIVE_PATH)

    def test_second_run_is_idempotent(self) -> None:
        ensure_ports_gamelist(self.ports)
        before = (self.ports / "gamelist.xml").read_bytes()

        result = ensure_ports_gamelist(self.ports)

        self.assertEqual(result["status"], "current")
        self.assertEqual((self.ports / "gamelist.xml").read_bytes(), before)

    def test_refuses_to_write_metadata_when_marquee_is_missing(self) -> None:
        (self.ports / "images" / "batocera-drone_marquee.png").unlink()

        with self.assertRaises(FileNotFoundError):
            ensure_ports_gamelist(self.ports)
        self.assertFalse((self.ports / "gamelist.xml").exists())

    def test_malformed_existing_gamelist_is_never_replaced(self) -> None:
        gamelist = self.ports / "gamelist.xml"
        original = b"<gameList><game>broken"
        gamelist.write_bytes(original)

        with self.assertRaises(ET.ParseError):
            ensure_ports_gamelist(self.ports)
        self.assertEqual(gamelist.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
