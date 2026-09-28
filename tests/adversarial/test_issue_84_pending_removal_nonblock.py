"""Adversarial tests for issue #84: pending-removal retry must not block on a non-regular occupant.

A named FIFO (mkfifo) dropped onto a pending-removal .torrent path would cause
os.open(O_RDONLY) to block until a writer appears, which may never happen.
Since _retry_pending_removal_torrent_files_locked runs under TorrentManager._lock
on the poller thread, one planted FIFO is a denial-of-service of the whole
torrent poller.

Fix: add O_NONBLOCK to open flags so ENXIO and a non-regular fd are identity
mismatches, not retryable busy conditions.
"""

import errno
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.transfer.torrent_manager import (
    _pending_removal_open_flags,
    _retry_unlink_pending_removal_file,
    _stat_fingerprint,
)


class Issue84PendingRemovalNonblockTests(unittest.TestCase):
    """Issue #84: pending-removal retry must not block the poller on a non-regular occupant."""

    def test_pending_removal_open_flags_include_nonblock(self) -> None:
        """O_NONBLOCK must be present in _pending_removal_open_flags."""
        flags = _pending_removal_open_flags()
        # os.O_RDONLY is 0 on POSIX, so it cannot be detected with a bitmask.
        if hasattr(os, "O_NONBLOCK"):
            self.assertTrue(
                flags & os.O_NONBLOCK,
                "O_NONBLOCK is required so a named FIFO at a tombstoned "
                "path cannot stall the poller (issue #84)",
            )
        if hasattr(os, "O_NOFOLLOW"):
            self.assertTrue(
                flags & os.O_NOFOLLOW,
                "O_NOFOLLOW must be preserved to keep identity check on "
                "the directory entry itself (issue #80)",
            )

    def test_retry_unlink_helper_named_fifo_returns_mismatch_without_blocking(self) -> None:
        """Opening a named FIFO with O_NONBLOCK returns mismatch promptly, not blocking."""
        if not hasattr(os, "mkfifo"):
            self.skipTest("named FIFOs are not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "solo.torrent"
            path.write_bytes(b"original-bytes")
            fingerprint = _stat_fingerprint(os.stat(path))
            path.unlink()
            os.mkfifo(path)
            box: list = []

            def run() -> None:
                box.append(_retry_unlink_pending_removal_file(str(path.resolve()), fingerprint))

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(timeout=2.0)
            self.assertFalse(
                thread.is_alive(),
                "opening the named FIFO for pending-removal retry blocked "
                "instead of returning mismatch",
            )
            self.assertEqual(
                box,
                ["mismatch"],
                "helper should return mismatch for a non-regular file",
            )
            self.assertTrue(
                stat.S_ISFIFO(os.lstat(path).st_mode),
                "the FIFO must be left in place, not unlinked",
            )

    def test_retry_unlink_helper_enxio_on_open_is_mismatch_not_busy(self) -> None:
        """ENXIO from opening a socket/device with no peer is a mismatch, not busy."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "solo.torrent"
            path.write_bytes(b"original-bytes")
            fingerprint = _stat_fingerprint(os.stat(path))
            real_open = os.open

            def enxio_open(target, flags, *args, **kwargs):
                if os.fspath(target) == str(path):
                    raise OSError(errno.ENXIO, "No such device or address")
                return real_open(target, flags, *args, **kwargs)

            with mock.patch("os.open", enxio_open):
                status = _retry_unlink_pending_removal_file(str(path), fingerprint)

            self.assertEqual(
                status,
                "mismatch",
                "ENXIO should be treated as mismatch, not retried as busy",
            )
            self.assertTrue(path.exists(), "the file must be left in place")
            self.assertEqual(
                path.read_bytes(),
                b"original-bytes",
                "the file must be unmodified",
            )

    def test_retry_unlink_helper_non_regular_fd_is_mismatch(self) -> None:
        """A successfully opened non-regular fd (directory, FIFO, etc.) is a mismatch."""
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFOs not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            # Create a FIFO at the path, which will be a non-regular file when opened
            path = Path(tmp) / "solo.torrent"
            os.mkfifo(path)
            # Use the FIFO's current stat as the "old" fingerprint
            fingerprint = _stat_fingerprint(os.stat(path))

            # When we try to unlink with a non-regular file occupying the path,
            # it should return mismatch (not try to unlink)
            status = _retry_unlink_pending_removal_file(str(path), fingerprint)

            self.assertEqual(
                status,
                "mismatch",
                "a non-regular fd should be treated as mismatch",
            )
            self.assertTrue(
                stat.S_ISFIFO(os.lstat(path).st_mode),
                "the FIFO must be left in place",
            )


if __name__ == "__main__":
    unittest.main()
