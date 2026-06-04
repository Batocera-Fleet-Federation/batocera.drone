"""Tests for the real-time ROM filesystem watcher.

The watcher is best-effort: it must degrade to a no-op (never raise) when
inotify is unavailable, and — on Linux — must wake the poller when ROM files
change. The change-detection test only runs on Linux where inotify exists.
"""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.rom_fs_watcher import RomFilesystemWatcher


class RomFilesystemWatcherTests(unittest.TestCase):
    def test_start_returns_false_when_roms_root_missing(self) -> None:
        calls = []
        watcher = RomFilesystemWatcher(Path("/no/such/roms/root"), lambda: calls.append(1))
        # Must not raise and must report unavailable so the caller keeps polling.
        self.assertFalse(watcher.start())
        self.assertEqual(calls, [])

    @unittest.skipIf(sys.platform.startswith("linux"), "non-Linux fallback only")
    def test_start_returns_false_on_non_linux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            watcher = RomFilesystemWatcher(Path(tmp), lambda: None)
            self.assertFalse(watcher.start())

    @unittest.skipUnless(sys.platform.startswith("linux"), "inotify is Linux-only")
    def test_detects_new_and_deleted_files_and_wakes_poller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            roms_root = Path(tmp)
            (roms_root / "snes").mkdir()
            fired = threading.Event()
            watcher = RomFilesystemWatcher(
                roms_root,
                fired.set,
                debounce_seconds=0.5,
                max_delay_seconds=2.0,
            )
            self.assertTrue(watcher.start())
            try:
                # New ROM appears.
                (roms_root / "snes" / "game.zip").write_bytes(b"rom-bytes")
                self.assertTrue(fired.wait(5.0), "watcher did not wake on new file")

                # New subdirectory is watched recursively, then a file inside it.
                fired.clear()
                (roms_root / "nes").mkdir()
                time.sleep(0.3)
                (roms_root / "nes" / "other.zip").write_bytes(b"more")
                self.assertTrue(fired.wait(5.0), "watcher did not wake on new subdir file")

                # Deletion also wakes the poller.
                fired.clear()
                (roms_root / "snes" / "game.zip").unlink()
                self.assertTrue(fired.wait(5.0), "watcher did not wake on delete")
            finally:
                watcher.stop()


if __name__ == "__main__":
    unittest.main()
