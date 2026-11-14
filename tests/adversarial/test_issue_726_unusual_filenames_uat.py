"""Issue #726 UAT: ROM save discovery must cope with ordinary-but-unusual file names.

Issue text: "Only saves that exist for the rom on remote drones should be an option
for download." and "This should work for all systems and roms."

Linux filesystems (Batocera userdata) allow ':' and '\\' in file names, and real ROM
titles contain them (for example "Zelda: Link's Awakening.gb").
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import drone_api
from app.common.settings import Settings
from app.transfer import game_saves as saves


class UnusualFilenameSaveDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.settings = SimpleNamespace(saves_root=self.root / "saves", userdata_root=self.root, device_id="local")
        (self.settings.saves_root / "snes").mkdir(parents=True)

    def _item(self, filename):
        stem = filename.rsplit(".", 1)[0]
        return {"system": "snes", "rom": filename, "stem": stem, "filename": filename, "ids": []}

    def test_unrelated_save_with_colon_does_not_hide_this_roms_save(self):
        """Regression for 'Only saves that exist for the rom ... should be an option':
        another game's save named with ':' must not make this ROM's own save vanish
        (or make the whole listing fail)."""
        folder = self.settings.saves_root / "snes"
        (folder / "Mario.srm").write_bytes(b"mario progress")
        (folder / "Zelda: Link's Awakening.srm").write_bytes(b"other game")
        result = saves.inventory(self.settings, self._item("Mario.sfc"))
        self.assertEqual([group["path"] for group in result], ["Mario.srm"])

    def test_unrelated_save_with_backslash_does_not_hide_this_roms_save(self):
        folder = self.settings.saves_root / "snes"
        (folder / "Mario.srm").write_bytes(b"mario progress")
        (folder / "Back\\Slash.srm").write_bytes(b"other game")
        result = saves.inventory(self.settings, self._item("Mario.sfc"))
        self.assertEqual([group["path"] for group in result], ["Mario.srm"])

    def test_rom_with_colon_in_its_title_can_list_its_own_save(self):
        """'This should work for all systems and roms.' -- a ROM whose file name
        contains ':' is still a ROM."""
        env = {
            "USERDATA_ROOT": str(self.root),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.db"),
            "DRONE_DEVICE_ID": "adversarial-726",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
        rom = settings.roms_root / "snes" / "Zelda: Link's Awakening.sfc"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"x" * 64)
        (self.root / "saves" / "snes" / "Zelda: Link's Awakening.srm").write_bytes(b"progress")
        repository = drone_api.RomRepository(settings.roms_root, settings.bios_root, settings=settings)
        _, roms = repository.list_assets("snes", "roms")
        self.assertEqual(len(roms), 1)
        item = saves.game(repository, "snes", unique_id=roms[0]["unique_id"])
        listed = saves.inventory(settings, item)
        self.assertEqual([group["path"] for group in listed], ["Zelda: Link's Awakening.srm"])


if __name__ == "__main__":
    unittest.main()
