"""Tests for resolving a ROM by its gamelist ``<game id>`` (the P2P identity).

Overmind identifies ROMs only by ``(system, gamelist_id)`` after the gamelist-source
refactor; the sender maps that id back to its own ``<path>`` so a receiver can pull the
file without Overmind ever carrying a filesystem path.
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
            target, relative_path, entry_type = repo.resolve_rom_file_by_gamelist_id("snes", "2144")
            self.assertEqual(relative_path, "Zelda.zip")
            self.assertEqual(entry_type, "file")
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), b"rombytes")

    def test_resolves_unscraped_entry_by_normalized_path(self):
        # Entries without an id attribute fall back to the normalized <path>.
        tmp, repo = self._repo_with_rom()
        with tmp:
            target, relative_path, entry_type = repo.resolve_rom_file_by_gamelist_id("snes", "Metroid.zip")
            self.assertEqual(relative_path, "Metroid.zip")
            self.assertEqual(entry_type, "file")

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


if __name__ == "__main__":
    unittest.main()
