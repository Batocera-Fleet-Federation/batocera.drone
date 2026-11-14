"""Issue #726 UAT: Stale ROM cache entries on a peer must not hide orphaned saves
behind a 404 error.

Issue quote:
"Only saves that exist for the rom on remote drones should be an option for download."
"This should work for all systems and roms."
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import drone_api
from app.common.settings import Settings
from app.transfer import game_saves as saves
from app.web.handlers_game_saves import HandlersGameSavesMixin


class StaleCacheOrphanedSavesUat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        env = {
            "USERDATA_ROOT": str(self.root),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.db"),
            "DRONE_DEVICE_ID": "peer-batocera",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.settings = Settings.from_env()

    def test_stale_rom_cache_entry_still_allows_orphaned_save_discovery(self):
        rom = self.settings.roms_root / "snes" / "Remote Mario.sfc"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"SNES_ROM_PAYLOAD" * 16)
        save_path = self.settings.saves_root / "snes" / "Mario.srm"
        save_path.parent.mkdir(parents=True)
        save_path.write_bytes(b"orphaned-save-data")

        repository = drone_api.RomRepository(self.settings.roms_root, self.settings.bios_root, settings=self.settings)
        fingerprint = repository.build_fingerprint(rom)
        _, cached_rows = repository.list_assets("snes", "roms")
        # Simulate the ROM file being deleted on disk before the cache rescans.
        rom.unlink()

        handler = HandlersGameSavesMixin()
        handler.settings = self.settings
        handler.repository = repository
        handler._peer_request_authorized = mock.Mock(return_value=True)
        sent = {}
        handler._send_json = lambda code, body: sent.update(code=code, body=body)

        with mock.patch.object(repository, "list_assets", return_value=(self.settings.roms_root / "snes", cached_rows)):
            handler._handle_peer_game_saves(
                {"system": ["snes"], "rom": ["Mario.sfc"], "fingerprint": [fingerprint]}
            )

        self.assertEqual(sent["code"], 200, sent)
        self.assertEqual([g["path"] for g in sent["body"]["saves"]], ["Mario.srm"])


if __name__ == "__main__":
    unittest.main()
