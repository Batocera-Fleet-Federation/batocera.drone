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
from unittest import mock

import app.drone_api as drone_api
from app.common.settings import Settings
from app.roms.rom_fs_watcher import RomFilesystemWatcher


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


class StartRomMetadataWatcherTests(unittest.TestCase):
    """``_start_rom_metadata_watcher`` (drone_api.py) is the wiring that
    decides which trees get near-real-time inotify coverage. Real inotify
    behavior is already covered generically above; this just verifies the
    wiring itself covers ROMs, saves, *and* movies -- movies previously had
    no watcher at all, so a new/moved movie file sat invisible until the next
    periodic poll (see rom-scanner's _poll_rom_metadata_once)."""

    def tearDown(self) -> None:
        drone_api._ROM_METADATA_WATCHER = None
        drone_api._SAVES_METADATA_WATCHER = None
        drone_api._MOVIES_METADATA_WATCHER = None

    def test_watches_roms_saves_and_movies_roots_with_scoped_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "SAVES_ROOT": str(root / "saves"),
                    "MOVIES_ROOT": str(root / "movies"),
                    "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                    "DRONE_DEVICE_ID": "watcher-wiring-test",
                },
                clear=True,
            ):
                settings = Settings.from_env()

            watchers = []

            class FakeWatcher:
                def __init__(self, path, on_change, **kwargs):
                    self.path = Path(path)
                    self.on_change = on_change
                    watchers.append(self)

                def start(self) -> bool:
                    return True

            with mock.patch.object(drone_api, "RomFilesystemWatcher", FakeWatcher), \
                 mock.patch.object(drone_api._saves_store, "sync_saves_cache") as sync_saves, \
                 mock.patch.object(drone_api._movies_store, "sync_movies_cache") as sync_movies, \
                 mock.patch.object(drone_api._ROM_METADATA_WAKE, "set") as wake_roms:
                drone_api._start_rom_metadata_watcher(settings)
                self.assertEqual(
                    [watcher.path for watcher in watchers],
                    [settings.roms_root, settings.saves_root, settings.movies_root],
                )
                watchers[0].on_change()
                watchers[1].on_change()
                watchers[2].on_change()
                wake_roms.assert_called_once_with()
                sync_saves.assert_called_once_with(settings.saves_root)
                sync_movies.assert_called_once_with(settings.movies_root)
            self.assertIsInstance(drone_api._ROM_METADATA_WATCHER, FakeWatcher)
            self.assertIsInstance(drone_api._SAVES_METADATA_WATCHER, FakeWatcher)
            self.assertIsInstance(drone_api._MOVIES_METADATA_WATCHER, FakeWatcher)
            self.assertEqual(drone_api._MOVIES_METADATA_WATCHER.path, settings.movies_root)

    def test_movies_watcher_not_set_when_start_fails(self) -> None:
        # Best-effort: if inotify can't watch movies_root (missing dir, watch
        # limit, non-Linux, ...) the global must stay None so status reporting
        # and any future caller can't mistake it for an active watcher.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "SAVES_ROOT": str(root / "saves"),
                    "MOVIES_ROOT": str(root / "movies"),
                    "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                    "DRONE_DEVICE_ID": "watcher-wiring-test-2",
                },
                clear=True,
            ):
                settings = Settings.from_env()

            class FailingWatcher:
                def __init__(self, path, on_change, **kwargs):
                    pass

                def start(self) -> bool:
                    return False

            with mock.patch.object(drone_api, "RomFilesystemWatcher", FailingWatcher):
                drone_api._start_rom_metadata_watcher(settings)

            self.assertIsNone(drone_api._ROM_METADATA_WATCHER)
            self.assertIsNone(drone_api._SAVES_METADATA_WATCHER)
            self.assertIsNone(drone_api._MOVIES_METADATA_WATCHER)


if __name__ == "__main__":
    unittest.main()
