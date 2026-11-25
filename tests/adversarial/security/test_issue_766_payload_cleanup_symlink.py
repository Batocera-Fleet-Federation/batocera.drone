"""Issue #766: pending payload cleanup must not follow a symlink out of the download dir.

Regression: a move+cleanup source folder is retried with shutil.rmtree. The retry
checks only the final path component for a symlink and compares the parent
lexically, so replacing an ancestor directory with a symlink makes root delete
another directory that shares the torrent folder's name.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.common.settings import Settings  # noqa: E402
from app.transfer.torrent_manager import TorrentManager  # noqa: E402


def _settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root / "userdata"),
        "ROMS_ROOT": str(root / "userdata" / "roms"),
        "BIOS_ROOT": str(root / "userdata" / "bios"),
        "SAVES_ROOT": str(root / "userdata" / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "issue-766-cleanup-symlink",
        "LOG_DIR": str(root / "logs"),
    }
    (root / "userdata").mkdir(parents=True, exist_ok=True)
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings.from_env()


class PayloadCleanupSymlinkEscapeTests(unittest.TestCase):
    def _queue(self, manager: TorrentManager, folder: Path, download_dir: Path) -> None:
        with manager._lock:
            manager._queue_pending_payload_cleanup_locked(folder, str(download_dir))
        manager._payload_cleanup_next_attempt.clear()

    def test_replacing_download_dir_with_symlink_does_not_delete_outside(self) -> None:
        """A symlink at the recorded download directory must not redirect rmtree."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_settings(root), start_worker=False)
            download = (root / "downloads").resolve()
            folder = download / "saves"
            folder.mkdir(parents=True)
            (folder / "leftover").write_bytes(b"busy")
            library = (root / "library").resolve()
            sensitive = library / "saves"
            sensitive.mkdir(parents=True)
            precious = sensitive / "precious"
            precious.write_bytes(b"keep")

            self._queue(manager, folder, download)
            download.rename(root / "downloads.real")
            download.symlink_to(library, target_is_directory=True)
            self.assertFalse((download / "saves").is_symlink())
            self.assertTrue(download.is_symlink())

            manager._move_tick()

            self.assertTrue(precious.exists(), "cleanup followed a download-dir symlink and deleted another tree")
            self.assertTrue(sensitive.is_dir())
            self.assertTrue((root / "downloads.real" / "saves" / "leftover").exists())

    def test_replacing_ancestor_with_symlink_does_not_delete_outside(self) -> None:
        """A symlink above the recorded download directory must not redirect rmtree."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_settings(root), start_worker=False)
            mount = (root / "mount").resolve()
            download = mount / "downloads"
            folder = download / "saves"
            folder.mkdir(parents=True)
            (folder / "leftover").write_bytes(b"busy")
            library = (root / "library").resolve()
            sensitive = library / "downloads" / "saves"
            sensitive.mkdir(parents=True)
            precious = sensitive / "precious"
            precious.write_bytes(b"keep")

            self._queue(manager, folder, download)
            mount.rename(root / "mount.real")
            mount.symlink_to(library, target_is_directory=True)
            self.assertFalse((mount / "downloads").is_symlink())
            self.assertFalse((mount / "downloads" / "saves").is_symlink())
            self.assertTrue(mount.is_symlink())

            manager._move_tick()

            self.assertTrue(precious.exists(), "cleanup followed an ancestor symlink and deleted another tree")
            self.assertTrue(sensitive.is_dir())
            self.assertTrue((root / "mount.real" / "downloads" / "saves" / "leftover").exists())


if __name__ == "__main__":
    unittest.main()
