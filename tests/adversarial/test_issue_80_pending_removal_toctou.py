"""Adversarial acceptance coverage for Issue #80: closing the pending-removal
retry's stat-to-unlink TOCTOU window with an fd/inode-held delete.

Issue #77 made the retry compare the tombstoned ``(st_dev, st_ino, st_size,
st_mtime_ns)`` identity against the path before unlinking. Issue #80 pointed
out that compare-then-``Path.unlink(path)`` still loses a replacement
swapped onto the name *between* the compare and the unlink. The shipped fix
(``_retry_unlink_pending_removal_file`` / ``_unlink_path_if_held_inode`` /
``_rename_aside_unlink_if_same_inode`` in ``app/transfer/torrent_manager.py``)
closes that by opening the tombstoned path, fstat-ing the fd, and only then
renaming the name aside to a private sibling and deleting *that* -- so a
replacement that wins the name after the rename is left alone.

This suite probes the failure/restore leg of that same rename-aside dance,
which is new code introduced by #80 itself and has its own TOCTOU: if a
legitimate replacement is dropped onto the name *after* the original has
been renamed aside but *before* the aside copy is actually unlinked, and
that unlink then fails (locked file, transient EBUSY, flaky removable
media -- exactly the class of failure #74/#77/#80 exist to tolerate), the
``except OSError`` handler blindly does ``os.rename(tmp, path)`` to restore
the original under its old name. On POSIX, ``os.rename`` onto an existing
destination is a silent atomic replace -- so that "restore" clobbers the
replacement that arrived in the interim, destroying exactly the file #80
was written to protect. A following successful retry (once the transient
failure clears) then unlinks the now-restored original, permanently losing
the replacement's content. This reproduces with the codebase's own
documented failure mode (locked/EBUSY file -- see the `matching identity
keeps tombstone when retry unlink still fails` case in the #77 suite and
`Path.unlink` still raising OSError per the drone-torrents-management
skill), not a contrived one.

Also probes two safety properties the #80 fix must have that its own tests
don't exercise: the identity check must not be foolable by a directory or a
symlink dropped onto the tombstoned name (no following, no unlinking), and
the module-level unlink guard must not let two managers retrying distinct
files at the same moment interfere with each other's target.
"""

from __future__ import annotations

import errno
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer.aria2_runtime import Aria2RpcError
from app.transfer.torrent_manager import (
    TorrentManager,
    _retry_unlink_pending_removal_file,
    _stat_fingerprint,
)


ORIGINAL_PAYLOAD = b"d8:announce0:4:infod4:name8:original4:ee"
REPLACEMENT_PAYLOAD = b"d8:announce0:4:infod4:name11:replaced11e"


def build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "issue-80-pending-removal-toctou-adversarial",
        "LOG_DIR": str(root / "logs"),
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class FakeRpc:
    """Stateful aria2 double so ticks after delete() reconcile like the
    real poll loop instead of merely recording method names."""

    def __init__(self) -> None:
        self.calls = []
        self.statuses = {}
        self.next_gid = 0

    def call(self, method, params=None, timeout=None):
        del timeout
        params = list(params or [])
        self.calls.append((method, params))

        if method in ("aria2.addTorrent", "aria2.addUri"):
            self.next_gid += 1
            gid = f"gid-{self.next_gid}"
            self.statuses[gid] = self._status(gid, status="paused")
            return gid
        if method == "aria2.tellStatus":
            gid = params[0]
            if gid not in self.statuses:
                raise Aria2RpcError(f"GID {gid} is not found")
            return dict(self.statuses[gid])
        if method in ("aria2.tellActive", "aria2.tellWaiting"):
            return [
                dict(status)
                for status in self.statuses.values()
                if status["status"] in ("active", "paused")
            ]
        if method in ("aria2.forceRemove", "aria2.removeDownloadResult"):
            gid = params[0] if params else None
            if gid not in self.statuses:
                raise Aria2RpcError(f"GID#{gid} is not found in the queue.")
            del self.statuses[gid]
            return "OK"
        return "OK"

    @staticmethod
    def _status(gid, status):
        return {
            "gid": gid,
            "status": status,
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        }


class FakeDaemon:
    def __init__(self, rpc: FakeRpc) -> None:
        self.rpc = rpc
        self.binary_path = "/fake/aria2c"
        self.last_error = ""
        self.bind_interface = None
        self.stopped = False

    @property
    def running(self):
        return not self.stopped

    def stop(self):
        self.stopped = True


def _write_torrent(directory: Path, name: str, payload: bytes = ORIGINAL_PAYLOAD) -> Path:
    path = directory / f"{name}.torrent"
    path.write_bytes(payload)
    return path


def _failing_unlink_for(torrent_path: Path):
    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.resolve() == torrent_path.resolve():
            raise OSError("Permission denied")
        return real_unlink(self, *args, **kwargs)

    return failing_unlink


class PendingRemovalTOCTOUTests(unittest.TestCase):
    def _make_manager(self, root: Path, rpc: FakeRpc | None = None) -> tuple:
        manager = TorrentManager(build_settings(root), start_worker=False)
        manager._daemon = FakeDaemon(rpc or FakeRpc())
        watch = root / "watch"
        manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 4})
        return manager, watch

    def _register(self, manager: TorrentManager, watch: Path, name: str, payload: bytes = ORIGINAL_PAYLOAD) -> tuple:
        torrent_path = _write_torrent(watch, name, payload)
        manager._tick()
        with manager._lock:
            entry_id, entry = next(
                (eid, e) for eid, e in manager._torrents.items() if Path(e["torrent_file"]).resolve() == torrent_path.resolve()
            )
        return entry_id, torrent_path, entry

    def _snapshot_names(self, manager: TorrentManager) -> list:
        return [e["name"] for e in manager.snapshot()["torrents"]]

    def _tombstone_via_delete(self, manager: TorrentManager, entry_id: str, torrent_path: Path) -> None:
        with mock.patch.object(Path, "unlink", _failing_unlink_for(torrent_path)):
            result = manager.delete(entry_id)
            self.assertEqual(result["status"], "deleted")
            self.assertFalse(result["torrent_file_removed"])
        self.assertTrue(torrent_path.exists())
        with manager._lock:
            self.assertIn(str(torrent_path.resolve()), manager._pending_removal_torrent_files)

    # ------------------------------------------------------------------
    # Critical: the rename-aside restore-on-failure leg clobbers a
    # legitimate replacement that lands during the aside window.
    # ------------------------------------------------------------------

    def _install_hostile_unlink(self, torrent_path: Path, dropped: list):
        """Simulate: the rename-aside succeeds (original is safely stashed
        under a private sibling name), a legitimate new .torrent is dropped
        onto the now-free original filename by an unrelated actor, and then
        the delete of the stashed-aside copy hits a transient failure (a
        locked file / EBUSY on flaky removable media -- the exact class of
        failure #74's tombstoning exists to tolerate). Only the rename-aside
        temp sibling is targeted; every other unlink passes through
        untouched, matching how ``real_unlink`` is used elsewhere in the
        guarded path."""
        real_unlink = os.unlink

        def hostile_unlink(target, *args, **kwargs):
            target_str = os.fspath(target)
            if "drone-pr-" in os.path.basename(target_str):
                if not dropped:
                    dropped.append(True)
                    torrent_path.write_bytes(REPLACEMENT_PAYLOAD)
                raise OSError(errno.EBUSY, "simulated transient lock")
            return real_unlink(target, *args, **kwargs)

        return hostile_unlink

    def test_retry_helper_restore_after_failed_aside_unlink_clobbers_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "solo.torrent"
            path.write_bytes(ORIGINAL_PAYLOAD)
            fingerprint = _stat_fingerprint(os.stat(path))

            dropped: list = []
            hostile = self._install_hostile_unlink(path, dropped)
            with mock.patch("os.unlink", hostile):
                status = _retry_unlink_pending_removal_file(str(path), fingerprint)

            self.assertTrue(dropped, "test setup did not reach the aside-unlink failure leg")
            self.assertEqual(status, "busy")
            self.assertTrue(path.exists(), "the replacement must not be deleted outright")
            self.assertEqual(
                path.read_bytes(),
                REPLACEMENT_PAYLOAD,
                "restoring the stashed-aside original after a failed delete must not "
                "silently replace a legitimate file that was dropped onto the name "
                "in the interim -- this is the same collateral-deletion issue #77/#80 "
                "exist to prevent, reintroduced by the rename-aside restore path",
            )

    def test_manager_tick_clobbers_then_permanently_deletes_legitimate_replacement(self) -> None:
        # Full-stack reproduction through the public TorrentManager surface,
        # then a second, unobstructed tick to show the damage compounds:
        # once the transient failure clears, the retry finds the (now
        # restored-to-original) file matching the tombstoned fingerprint
        # again and deletes it for real -- permanently losing whatever the
        # replacement contained.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "solo")
            self._tombstone_via_delete(manager, entry_id, torrent_path)

            dropped: list = []
            hostile = self._install_hostile_unlink(torrent_path, dropped)
            with mock.patch("os.unlink", hostile):
                manager._tick()

            self.assertTrue(dropped, "test setup did not reach the aside-unlink failure leg")
            self.assertTrue(
                torrent_path.exists(),
                "legitimate replacement must survive a failed pending-removal retry",
            )
            self.assertEqual(
                torrent_path.read_bytes(),
                REPLACEMENT_PAYLOAD,
                "the replacement dropped while the original was stashed aside for "
                "deletion must not be clobbered when that deletion fails and the "
                "handler restores the original back onto the name",
            )

            # Second tick, no induced failure this time: with the bug, the
            # restored original (which now sits at the path with its
            # original identity) matches the tombstoned fingerprint again
            # and gets deleted for real -- the replacement is gone for good.
            manager._tick()
            self.assertTrue(
                torrent_path.exists() and torrent_path.read_bytes() == REPLACEMENT_PAYLOAD,
                "replacement content must still be present once the transient "
                "failure clears; if it was clobbered back to the original on the "
                "first tick, a routine follow-up retry now permanently deletes it",
            )

    # ------------------------------------------------------------------
    # Robustness checks: the identity gate must not be foolable by a
    # directory or a symlink swapped onto the tombstoned name.
    # ------------------------------------------------------------------

    def test_retry_helper_leaves_directory_swapped_onto_path_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "solo.torrent"
            path.write_bytes(ORIGINAL_PAYLOAD)
            fingerprint = _stat_fingerprint(os.stat(path))

            path.unlink()
            path.mkdir()
            marker = path / "keep-me.txt"
            marker.write_bytes(b"directory contents must survive")

            status = _retry_unlink_pending_removal_file(str(path), fingerprint)

            self.assertEqual(status, "mismatch")
            self.assertTrue(path.is_dir())
            self.assertTrue(marker.exists())
            self.assertEqual(marker.read_bytes(), b"directory contents must survive")

    def test_retry_helper_never_follows_or_unlinks_symlink_swapped_onto_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "solo.torrent"
            path.write_bytes(ORIGINAL_PAYLOAD)
            fingerprint = _stat_fingerprint(os.stat(path))

            sentinel = root / "sensitive.txt"
            sentinel.write_bytes(b"do-not-touch")

            path.unlink()
            path.symlink_to(sentinel)

            status = _retry_unlink_pending_removal_file(str(path), fingerprint)

            self.assertEqual(status, "mismatch")
            self.assertTrue(path.is_symlink(), "the symlink node itself must not be unlinked")
            self.assertTrue(sentinel.exists(), "the symlink target must never be touched")
            self.assertEqual(sentinel.read_bytes(), b"do-not-touch")

    def test_manager_symlink_swapped_between_ticks_is_left_for_the_scan(self) -> None:
        # Same property, exercised end-to-end: a symlink dropped onto a
        # tombstoned watch-folder path between ticks must never be unlinked
        # or followed by the retry, matching the "identity mismatch -> drop
        # tombstone without unlinking" rule for any non-matching replacement
        # (issue #77), including one that isn't a regular file at all.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "solo")
            self._tombstone_via_delete(manager, entry_id, torrent_path)

            sentinel = root / "sensitive.txt"
            sentinel.write_bytes(b"do-not-touch")
            torrent_path.unlink()
            torrent_path.symlink_to(sentinel)

            manager._tick()

            self.assertTrue(torrent_path.is_symlink())
            self.assertTrue(sentinel.exists())
            self.assertEqual(sentinel.read_bytes(), b"do-not-touch")
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])

    # ------------------------------------------------------------------
    # Concurrency: the module-level unlink guard must not let one
    # manager's retry interfere with a different manager's target file.
    # ------------------------------------------------------------------

    def test_concurrent_ticks_across_two_managers_do_not_cross_contaminate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager_a, watch_a = self._make_manager(root / "device-a")
            manager_b, watch_b = self._make_manager(root / "device-b")

            entry_a, path_a, _ = self._register(manager_a, watch_a, "alpha")
            entry_b, path_b, _ = self._register(manager_b, watch_b, "beta")
            self._tombstone_via_delete(manager_a, entry_a, path_a)
            self._tombstone_via_delete(manager_b, entry_b, path_b)

            barrier = threading.Barrier(2)
            errors: list = []

            def run(manager, barrier_):
                try:
                    barrier_.wait(timeout=5)
                    manager._tick()
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)

            t_a = threading.Thread(target=run, args=(manager_a, barrier))
            t_b = threading.Thread(target=run, args=(manager_b, barrier))
            t_a.start()
            t_b.start()
            t_a.join(timeout=10)
            t_b.join(timeout=10)

            self.assertEqual(errors, [])
            self.assertFalse(path_a.exists())
            self.assertFalse(path_b.exists())
            with manager_a._lock:
                self.assertEqual(manager_a._pending_removal_torrent_files, [])
            with manager_b._lock:
                self.assertEqual(manager_b._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(manager_a), [])
            self.assertEqual(self._snapshot_names(manager_b), [])


if __name__ == "__main__":
    unittest.main()
