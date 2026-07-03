"""Regression: artwork must converge so the metadata poller stops re-uploading it.

Previously `_poll_rom_metadata_cache` stored the raw scanned artwork dict, which never
equaled the round-tripped cache row, so every artwork was re-queued as "changed" on every
poll and the full artwork set was re-uploaded forever (a CPU-pinning resync loop that hung
the drone's local web UI). After normalizing scanned artwork to the canonical
ArtworkCacheRow payload shape, a second poll over unchanged art must queue nothing.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.drone_api import RomRepository, Settings, _poll_rom_metadata_cache
from app.storage.rom_metadata_store import (
    _clear_pending_rom_metadata_changes,
    _read_pending_rom_metadata_changes,
)


class ArtworkConvergenceTest(unittest.TestCase):
    def _settings_with_artwork(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name) / "userdata"
        system = root / "roms" / "snes"
        system.mkdir(parents=True)
        (system / "Game.zip").write_bytes(b"rom-bytes")
        (system / "images").mkdir()
        (system / "images" / "Game.png").write_bytes(b"art-bytes")
        # A gamelist game carrying an <image> tag is what list_artwork_metadata discovers.
        (system / "gamelist.xml").write_text(
            "<gameList><game><path>./Game.zip</path><name>Game</name>"
            "<image>./images/Game.png</image></game></gameList>",
            encoding="utf-8",
        )
        with mock.patch.dict(
            "os.environ",
            {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
            clear=True,
        ):
            settings = Settings.from_env()
        return tmp, settings, RomRepository(settings.roms_root, settings.bios_root)

    def test_artwork_not_requeued_on_unchanged_rescan(self):
        tmp, settings, repo = self._settings_with_artwork()
        with tmp:
            # First poll discovers the artwork and queues it (the legitimate first upload).
            _poll_rom_metadata_cache(settings, repo)
            first = _read_pending_rom_metadata_changes(settings)
            self.assertGreaterEqual(len(first["artwork"]), 1, "test must actually discover artwork")

            # Simulate a successful upload clearing the change queue.
            _clear_pending_rom_metadata_changes(settings)

            # Second poll over identical on-disk artwork must queue nothing (convergence).
            _poll_rom_metadata_cache(settings, repo)
            second = _read_pending_rom_metadata_changes(settings)
            self.assertEqual(second["artwork"], [], "unchanged artwork was re-queued (resync loop)")


if __name__ == "__main__":
    unittest.main()
