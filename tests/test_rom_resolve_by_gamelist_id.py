"""Tests for resolving a ROM by its gamelist ``<game id>`` (the P2P identity).

Peers identify ROMs only by ``(system, gamelist_id)`` after the gamelist-source
refactor; the sender maps that id back to its own ``<path>`` so a receiver can pull the
file without either peer ever carrying the other's filesystem path.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.drone_api import RomRepository


class ResolveRomByGamelistIdTest(unittest.TestCase):
    def _repo_with_rom(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name) / "userdata"
        system = root / "roms" / "snes"
        rom = system / "Zelda.zip"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"rombytes")
        (system / "gamelist.xml").write_text(
            "<gameList>"
            "<game id='2144'><path>./Zelda.zip</path><name>Zelda</name></game>"
            "<game><path>./Metroid.zip</path><name>Metroid</name></game>"
            "</gameList>",
            encoding="utf-8",
        )
        (system / "Metroid.zip").write_bytes(b"metroid")
        with mock.patch.dict(
            "os.environ",
            {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
            clear=True,
        ):
            settings = Settings.from_env()
        return tmp, RomRepository(settings.roms_root, settings.bios_root)

    def test_resolves_by_id_attribute(self):
        tmp, repo = self._repo_with_rom()
        with tmp:
            target, relative_path, entry_type, marker = repo.resolve_rom_file_by_gamelist_id("snes", "2144")
            self.assertEqual(relative_path, "Zelda.zip")
            self.assertEqual(entry_type, "file")
            self.assertEqual(marker, "Zelda.zip")
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), b"rombytes")

    def test_resolves_unscraped_entry_by_normalized_path(self):
        # Entries without an id attribute fall back to the normalized <path>.
        tmp, repo = self._repo_with_rom()
        with tmp:
            target, relative_path, entry_type, marker = repo.resolve_rom_file_by_gamelist_id("snes", "Metroid.zip")
            self.assertEqual(relative_path, "Metroid.zip")
            self.assertEqual(entry_type, "file")
            self.assertEqual(marker, "Metroid.zip")

    def test_unknown_id_raises_not_found(self):
        tmp, repo = self._repo_with_rom()
        with tmp:
            with self.assertRaises(FileNotFoundError):
                repo.resolve_rom_file_by_gamelist_id("snes", "9999")

    def test_blank_id_raises_value_error(self):
        tmp, repo = self._repo_with_rom()
        with tmp:
            with self.assertRaises(ValueError):
                repo.resolve_rom_file_by_gamelist_id("snes", "")

    def test_missing_rom_file_raises_not_found(self):
        # gamelist entry exists but the referenced file was deleted.
        tmp, repo = self._repo_with_rom()
        with tmp:
            (repo.get_system_dir("snes") / "Zelda.zip").unlink()
            with self.assertRaises(FileNotFoundError):
                repo.resolve_rom_file_by_gamelist_id("snes", "2144")

    def _repo_with_folder_unit_rom(self, system_name):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name) / "userdata"
        system = root / "roms" / system_name
        game_dir = system / "Sonic Adventure (USA)"
        game_dir.mkdir(parents=True)
        (game_dir / "Sonic Adventure (USA).gdi").write_bytes(b"gdi index")
        (game_dir / "track01.bin").write_bytes(b"track one bytes")
        (game_dir / "track02.bin").write_bytes(b"track two bytes")
        (system / "gamelist.xml").write_text(
            "<gameList>"
            "<game id='77'><path>./Sonic Adventure (USA)/Sonic Adventure (USA).gdi</path><name>Sonic</name></game>"
            "</gameList>",
            encoding="utf-8",
        )
        with mock.patch.dict(
            "os.environ",
            {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
            clear=True,
        ):
            settings = Settings.from_env()
        return tmp, RomRepository(settings.roms_root, settings.bios_root)

    def test_folder_unit_system_resolves_marker_to_folder(self):
        # A table system (dreamcast): the .gdi entry resolves to its per-game folder
        # as the transfer unit, keeping the marker path for identity/artwork.
        tmp, repo = self._repo_with_folder_unit_rom("dreamcast")
        with tmp:
            target, relative_path, entry_type, marker = repo.resolve_rom_file_by_gamelist_id("dreamcast", "77")
            self.assertEqual(entry_type, "folder")
            self.assertEqual(relative_path, "Sonic Adventure (USA)")
            self.assertEqual(marker, "Sonic Adventure (USA)/Sonic Adventure (USA).gdi")
            self.assertTrue(target.is_dir())

    def test_nested_marker_resolves_to_top_level_folder(self):
        # Lindbergh: marker nested in elf/ beside the fs/ data dir -- the transfer
        # unit is the whole top-level game folder, not the marker's parent.
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name) / "userdata"
        system = root / "roms" / "lindbergh"
        (system / "hotd4a" / "elf").mkdir(parents=True)
        (system / "hotd4a" / "elf" / "hotd4a.game").write_bytes(b"marker")
        (system / "hotd4a" / "fs").mkdir()
        (system / "hotd4a" / "fs" / "disk0.bin").write_bytes(b"f" * 64)
        (system / "gamelist.xml").write_text(
            "<gameList><game id='5'><path>./hotd4a/elf/hotd4a.game</path><name>HOTD4</name></game></gameList>",
            encoding="utf-8",
        )
        with mock.patch.dict(
            "os.environ",
            {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
            clear=True,
        ):
            settings = Settings.from_env()
        repo = RomRepository(settings.roms_root, settings.bios_root)
        with tmp:
            target, relative_path, entry_type, marker = repo.resolve_rom_file_by_gamelist_id("lindbergh", "5")
            self.assertEqual(entry_type, "folder")
            self.assertEqual(relative_path, "hotd4a")
            self.assertEqual(marker, "hotd4a/elf/hotd4a.game")
            self.assertTrue((target / "fs" / "disk0.bin").is_file())

    def test_non_table_system_keeps_single_file(self):
        # The same layout under a system NOT in the folder-unit table stays a file.
        tmp, repo = self._repo_with_folder_unit_rom("snes")
        with tmp:
            target, relative_path, entry_type, marker = repo.resolve_rom_file_by_gamelist_id("snes", "77")
            self.assertEqual(entry_type, "file")
            self.assertEqual(relative_path, "Sonic Adventure (USA)/Sonic Adventure (USA).gdi")
            self.assertEqual(marker, relative_path)
            self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
