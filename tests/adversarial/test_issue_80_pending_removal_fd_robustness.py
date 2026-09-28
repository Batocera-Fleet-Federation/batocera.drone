"""Adversarial acceptance coverage for Issue #80's fd/inode-held delete
mechanism (``_retry_unlink_pending_removal_file`` / ``_unlink_path_if_held_inode``
/ ``_rename_aside_unlink_if_same_inode`` in ``app/transfer/torrent_manager.py``).

The other #80 suite (``test_issue_80_pending_removal_toctou.py``) probes the
rename-aside restore-on-failure leg and the directory/symlink identity gate.
This suite targets three properties of the *mechanism itself* that neither
that suite nor ``tests/test_torrents.py`` exercises:

1. Every new failure surface the fd-based rewrite introduces --
   ``os.open`` raising something other than ``FileNotFoundError`` (e.g. a
   permission error), ``os.fstat`` failing on an already-open fd, and
   ``os.rename`` failing on the *outer* rename-aside move with something
   other than ``FileNotFoundError`` -- must degrade to ``"busy"`` (keep the
   tombstone, leave the file alone) rather than raising or silently
   dropping the tombstone. The existing suites only induce failure via a
   patched ``Path.unlink``, never via the new ``os.open``/``os.fstat`` call
   sites this issue added.
2. The retry helper opens an fd for every outcome (``gone``, ``mismatch``,
   ``busy``, ``unlinked``) and must close it on every one of those paths --
   a background poller that retries a stuck tombstone every tick for the
   life of the process cannot afford to leak a descriptor per tick.
3. ``_unlink_path_if_held_inode`` monkeypatches ``os.unlink`` (and the
   pathlib accessor) *process-wide* for the duration of one retry. An
   unrelated thread deleting a completely different file while that guard
   is installed must still have its delete go through untouched -- the
   guard's file-identity check, not blanket interception, is what must do
   the filtering.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.transfer.torrent_manager import (
    _retry_unlink_pending_removal_file,
    _stat_fingerprint,
)


def _lowest_free_fd() -> int:
    fd = os.open(os.devnull, os.O_RDONLY)
    os.close(fd)
    return fd


class PendingRemovalFdErrorPathTests(unittest.TestCase):
    """New os.open/os.fstat/os.rename failure surfaces must degrade to
    "busy", never raise and never drop the tombstone."""

    def test_permission_denied_opening_path_returns_busy_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "solo.torrent"
            path.write_bytes(b"original")
            fingerprint = _stat_fingerprint(os.stat(path))
            real_open = os.open

            def denying_open(target, flags, *args, **kwargs):
                if os.fspath(target) == str(path):
                    raise PermissionError(13, "Permission denied")
                return real_open(target, flags, *args, **kwargs)

            with mock.patch("os.open", denying_open):
                status = _retry_unlink_pending_removal_file(str(path), fingerprint)

            self.assertEqual(status, "busy")
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), b"original")

    def test_fstat_failure_on_open_fd_returns_busy_and_still_closes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "solo.torrent"
            path.write_bytes(b"original")
            fingerprint = _stat_fingerprint(os.stat(path))

            baseline = _lowest_free_fd()
            with mock.patch("os.fstat", side_effect=OSError(5, "simulated I/O error")):
                status = _retry_unlink_pending_removal_file(str(path), fingerprint)
            after = _lowest_free_fd()

            self.assertEqual(status, "busy")
            self.assertTrue(path.exists(), "a failed fstat must never lose the file")
            self.assertEqual(
                after,
                baseline,
                "the fd opened for the fstat attempt must be closed even when "
                "fstat itself raises, or every stuck tombstone leaks one "
                "descriptor per poll tick for the life of the process",
            )

    def test_rename_aside_permission_error_leaves_file_in_place_and_stays_busy(self) -> None:
        # The outer os.rename(path, tmp) inside _rename_aside_unlink_if_same_inode
        # only special-cases FileNotFoundError. A general OSError (read-only
        # parent directory, EACCES, EROFS on a remounted watch folder) must
        # still surface as "busy" with the original untouched -- not raise
        # out of the retry loop and not delete anything.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "solo.torrent"
            path.write_bytes(b"original")
            fingerprint = _stat_fingerprint(os.stat(path))
            real_rename = os.rename

            def denying_rename(src, dst, *args, **kwargs):
                if "drone-pr-" in os.path.basename(os.fspath(dst)):
                    raise PermissionError(13, "Permission denied")
                return real_rename(src, dst, *args, **kwargs)

            with mock.patch("os.rename", denying_rename):
                status = _retry_unlink_pending_removal_file(str(path), fingerprint)

            self.assertEqual(status, "busy")
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), b"original")


class PendingRemovalFdLeakTests(unittest.TestCase):
    """A long-running poller retries every stuck tombstone on every tick;
    any outcome that leaks a descriptor is a slow resource exhaustion bug."""

    def test_no_fd_leak_across_repeated_gone_mismatch_busy_and_unlinked_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gone_path = root / "gone.torrent"  # never created

            mismatch_path = root / "mismatch.torrent"
            mismatch_path.write_bytes(b"a")
            stale_fingerprint = _stat_fingerprint(os.stat(mismatch_path))
            mismatch_path.write_bytes(b"a-different-length-payload")

            baseline = _lowest_free_fd()

            for _ in range(25):
                status = _retry_unlink_pending_removal_file(str(gone_path), (0, 0, 0, 0))
                self.assertEqual(status, "gone")

                status = _retry_unlink_pending_removal_file(str(mismatch_path), stale_fingerprint)
                self.assertEqual(status, "mismatch")

                busy_path = root / "busy.torrent"
                busy_path.write_bytes(b"busy")
                busy_fingerprint = _stat_fingerprint(os.stat(busy_path))
                with mock.patch("os.fstat", side_effect=OSError(5, "simulated I/O error")):
                    status = _retry_unlink_pending_removal_file(str(busy_path), busy_fingerprint)
                self.assertEqual(status, "busy")
                busy_path.unlink()

                unlink_path = root / "unlink.torrent"
                unlink_path.write_bytes(b"to-delete")
                unlink_fingerprint = _stat_fingerprint(os.stat(unlink_path))
                status = _retry_unlink_pending_removal_file(str(unlink_path), unlink_fingerprint)
                self.assertEqual(status, "unlinked")
                self.assertFalse(unlink_path.exists())

            after = _lowest_free_fd()
            self.assertEqual(
                after,
                baseline,
                "repeated gone/mismatch/busy/unlinked retries leaked at least "
                "one file descriptor",
            )


class PendingRemovalGuardBlastRadiusTests(unittest.TestCase):
    """_unlink_path_if_held_inode patches os.unlink process-wide for the
    duration of one retry. Filtering must come from the identity check
    inside the guard, not from accidentally blocking unrelated deletes
    that happen to run while the guard is installed."""

    def test_unrelated_concurrent_unlink_during_guard_window_still_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "solo.torrent"
            target.write_bytes(b"original")
            fingerprint = _stat_fingerprint(os.stat(target))

            other = root / "unrelated.txt"
            other.write_bytes(b"do-not-touch-my-deletion")

            guard_active = threading.Event()
            other_deleted = threading.Event()
            real_lstat = os.lstat
            triggered = []

            def delaying_lstat(p, *args, **kwargs):
                result = real_lstat(p, *args, **kwargs)
                if not triggered and os.fspath(p) == str(target):
                    triggered.append(True)
                    guard_active.set()
                    if not other_deleted.wait(timeout=5):
                        raise AssertionError(
                            "the unrelated-file thread never finished its "
                            "delete within the guard window"
                        )
                return result

            def delete_other():
                if not guard_active.wait(timeout=5):
                    return
                os.unlink(other)
                other_deleted.set()

            thread = threading.Thread(target=delete_other)
            thread.start()
            try:
                with mock.patch("os.lstat", delaying_lstat):
                    status = _retry_unlink_pending_removal_file(str(target), fingerprint)
            finally:
                thread.join(timeout=5)

            self.assertTrue(triggered, "test setup never reached the guard window")
            self.assertFalse(thread.is_alive(), "unrelated-file thread did not finish")
            self.assertEqual(status, "unlinked")
            self.assertFalse(target.exists(), "the tombstoned file must still be removed")
            self.assertFalse(
                other.exists(),
                "an unrelated file's own delete, issued by another thread while "
                "the pending-removal guard was installed, must still take "
                "effect -- the guard must filter by identity, not by blocking "
                "every unlink in the process while it runs",
            )


if __name__ == "__main__":
    unittest.main()
