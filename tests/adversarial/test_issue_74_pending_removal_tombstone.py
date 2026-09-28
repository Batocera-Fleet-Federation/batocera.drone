"""Adversarial acceptance coverage for Issue #74: when unlinking a watched
``.torrent`` file fails with ``OSError`` (read-only watch dir, file locked by
another process, etc.) during ``delete()``/``remove_from_list()``/bulk
``clear()``/move+cleanup, ``TorrentManager`` must not let the surviving file
get rediscovered as a brand-new queued row on the very next
``_scan_watch_directory_locked`` tick.

The landed fix (``app/transfer/torrent_manager.py``) adds
``_queue_pending_removal_torrent_file`` (tombstones the resolved path in
``self._pending_removal_torrent_files`` on unlink failure) and
``_retry_pending_removal_torrent_files_locked`` (re-attempts the unlink on
every scan, called unconditionally at the *top* of
``_scan_watch_directory_locked`` -- before the directory is scanned for new
candidates -- and before the tombstoned path is even checked against
``known_files``).

These tests independently drive that mechanism through cases the landing
regression test (``tests/test_torrents.py``,
``test_clear_delete_from_ui_unlink_failure_does_not_resurrect_row_on_next_tick``)
does not cover:

* ``delete()`` (single-entry path), not just bulk ``clear()`` -- a different
  call site that also calls ``_queue_pending_removal_torrent_file``.
* Repeated failures must not duplicate the tombstone entry.
* The tombstone list is persisted and must survive a manager restart
  (fresh ``TorrentManager`` loading the same state file) without forgetting
  an outstanding pending removal.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer.aria2_runtime import Aria2RpcError
from app.transfer.torrent_manager import TorrentManager


def build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "issue-74-pending-removal-tombstone-adversarial",
        "LOG_DIR": str(root / "logs"),
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class FakeRpc:
    """Stateful aria2 double: forceRemove/tellStatus reflect a real daemon
    inventory so ticks after delete()/clear() behave like the real
    reconciliation loop instead of merely recording that a method was
    called."""

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
            return [dict(status) for status in self.statuses.values() if status["status"] in ("active", "paused")]
        if method in ("aria2.forceRemove", "aria2.removeDownloadResult"):
            gid = params[0] if params else None
            if gid not in self.statuses:
                raise Aria2RpcError(f"GID#{gid} is not found in the queue.")
            del self.statuses[gid]
            return "OK"
        return "OK"

    def method_calls(self, name):
        return [params for method, params in self.calls if method == name]

    def _status(self, gid, status):
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


def _write_torrent(directory: Path, name: str, payload: bytes = b"d8:announce0:e") -> Path:
    path = directory / f"{name}.torrent"
    path.write_bytes(payload)
    return path


class PendingRemovalTorrentFileTests(unittest.TestCase):
    def _make_manager(self, root: Path, rpc: FakeRpc) -> tuple:
        manager = TorrentManager(build_settings(root), start_worker=False)
        manager._daemon = FakeDaemon(rpc)
        watch = root / "watch"
        manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 2})
        return manager, watch

    def test_single_delete_unlink_failure_tombstones_and_does_not_resurrect(self) -> None:
        # delete() is a distinct call site from clear() and independently
        # calls _queue_pending_removal_torrent_file on OSError. The landing
        # regression test only drives clear(); this closes that gap.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, watch = self._make_manager(root, rpc)
            _write_torrent(watch, "solo")
            manager._tick()
            with manager._lock:
                entry_id, entry = next(iter(manager._torrents.items()))
                torrent_path = Path(entry["torrent_file"])
            self.assertTrue(torrent_path.exists())

            real_unlink = Path.unlink

            def failing_unlink(self, *args, **kwargs):
                if self.resolve() == torrent_path.resolve():
                    raise OSError("Permission denied")
                return real_unlink(self, *args, **kwargs)

            with mock.patch.object(Path, "unlink", failing_unlink):
                result = manager.delete(entry_id)
                self.assertEqual(result["status"], "deleted")
                self.assertFalse(result["torrent_file_removed"])
                self.assertTrue(torrent_path.exists())
                self.assertEqual(manager.snapshot()["torrents"], [])

                with manager._lock:
                    self.assertEqual(
                        manager._pending_removal_torrent_files,
                        [str(torrent_path.resolve())],
                    )

                # The very next tick's watch-folder rescan must not
                # rediscover the still-present file as a brand-new entry.
                manager._tick()
                self.assertEqual(manager.snapshot()["torrents"], [])
                self.assertTrue(torrent_path.exists())

            # Once the transient failure clears, the retry pass in the next
            # scan finally unlinks the file and drops the tombstone.
            manager._tick()
            self.assertFalse(torrent_path.exists())
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])

    def test_repeated_unlink_failures_do_not_duplicate_tombstone_entry(self) -> None:
        # _queue_pending_removal_torrent_file must dedupe: calling it twice
        # for the same resolved path (e.g. two separate failed delete()
        # attempts before the transient failure clears) must not grow the
        # pending list unboundedly.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, watch = self._make_manager(root, rpc)
            _write_torrent(watch, "flaky")
            manager._tick()
            with manager._lock:
                entry_id, entry = next(iter(manager._torrents.items()))
                torrent_path = Path(entry["torrent_file"])

            with mock.patch.object(Path, "unlink", side_effect=OSError("Permission denied")):
                manager.delete(entry_id)
                with manager._lock:
                    self.assertEqual(len(manager._pending_removal_torrent_files), 1)

                # A second tick retries the unlink (still failing) -- the
                # tombstone list must still contain exactly one entry, not
                # accumulate a duplicate.
                manager._tick()
                with manager._lock:
                    self.assertEqual(
                        manager._pending_removal_torrent_files,
                        [str(torrent_path.resolve())],
                    )

    def test_pending_removal_tombstone_survives_persist_reload(self) -> None:
        # self._pending_removal_torrent_files is persisted alongside
        # pending_removal_gids/_info_hashes. A restart (fresh TorrentManager
        # loading the same state file) must not forget an outstanding
        # tombstone and let the file resurrect on the new process's first
        # scan.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, watch = self._make_manager(root, rpc)
            _write_torrent(watch, "restart-me")
            manager._tick()
            with manager._lock:
                entry_id, entry = next(iter(manager._torrents.items()))
                torrent_path = Path(entry["torrent_file"])

            with mock.patch.object(Path, "unlink", side_effect=OSError("Permission denied")):
                manager.delete(entry_id)

            with manager._lock:
                self.assertEqual(
                    manager._pending_removal_torrent_files,
                    [str(torrent_path.resolve())],
                )

            reloaded = TorrentManager(build_settings(root), start_worker=False)
            reloaded._daemon = FakeDaemon(FakeRpc())
            with reloaded._lock:
                self.assertEqual(
                    reloaded._pending_removal_torrent_files,
                    [str(torrent_path.resolve())],
                )

            self.assertTrue(torrent_path.exists())
            reloaded._tick()
            self.assertEqual(reloaded.snapshot()["torrents"], [])

if __name__ == "__main__":
    unittest.main()
