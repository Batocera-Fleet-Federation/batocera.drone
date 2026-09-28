"""Adversarial acceptance coverage for Issue #71: bulk ``clear()`` with
``delete_from_ui`` (but no ``delete_torrent_file``) must not let a watched
.torrent file survive on disk and get rediscovered as a brand-new queued
entry by the next ``_scan_watch_directory_locked`` tick.

The fix under test (``app/transfer/torrent_manager.py``, ``clear()``) changed
the unlink guard from ``delete_torrent_file`` to
``delete_torrent_file or delete_from_ui``. These tests independently drive
that guard through combinations the landing test in ``tests/test_torrents.py``
(``test_clear_delete_from_ui_alone_unlinks_watched_torrent_file``) does not
cover: the ``scope="all"`` path (not just ``"completed"``), a non-complete
("downloading") target, a magnet-added entry with no watched file at all,
scope isolation (untouched entries must keep their files), an unexpected/
malformed ``scope`` value, and a truthy-but-non-bool ``delete_from_ui``
payload value, plus repeated/idempotent clear() calls.
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
        "DRONE_DEVICE_ID": "issue-71-clear-watch-reappear-adversarial",
        "LOG_DIR": str(root / "logs"),
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class FakeRpc:
    """Stateful aria2 double: forceRemove/tellStatus reflect a real daemon
    inventory so ticks after clear() behave like the real reconciliation
    loop instead of merely recording that a method was called."""

    def __init__(self) -> None:
        self.calls = []
        self.statuses = {}
        self.next_gid = 0

    def call(self, method, params=None, timeout=None):
        del timeout
        params = list(params or [])
        self.calls.append((method, params))

        if method == "aria2.addTorrent":
            self.next_gid += 1
            gid = f"gid-{self.next_gid}"
            self.statuses[gid] = self._status(gid, status="paused")
            return gid
        if method == "aria2.addUri":
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


def _write_torrent(directory: Path, name: str) -> Path:
    path = directory / f"{name}.torrent"
    path.write_bytes(b"d8:announce0:e")
    return path


class ClearWatchFileReappearTests(unittest.TestCase):
    def _make_manager(self, root: Path, rpc: FakeRpc) -> tuple:
        manager = TorrentManager(build_settings(root), start_worker=False)
        manager._daemon = FakeDaemon(rpc)
        watch = root / "watch"
        manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 2})
        return manager, watch

    def _mark_complete(self, manager: TorrentManager, name: str) -> None:
        with manager._lock:
            entry = next(e for e in manager._torrents.values() if e["name"] == name)
            gid = entry["gid"]
        rpc = manager._daemon.rpc
        rpc.statuses[gid].update({"status": "complete", "totalLength": "4", "completedLength": "4"})

    def test_scope_all_delete_from_ui_alone_unlinks_active_entry_watch_file(self) -> None:
        # The landing regression test only exercises scope="completed"; the
        # same reappearance bug is reachable for an in-progress (non-complete)
        # entry cleared under scope="all", since clear()'s target-selection
        # branches on scope independently of the unlink guard.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, watch = self._make_manager(root, rpc)
            _write_torrent(watch, "still-active")
            manager._tick()
            with manager._lock:
                self.assertEqual(manager._torrents[next(iter(manager._torrents))]["status"], "downloading")

            result = manager.clear({"delete_from_ui": True, "scope": "all"})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["cleared"], 1)
            self.assertEqual(list(watch.glob("*.torrent")), [])

            manager._tick()
            self.assertEqual(manager.snapshot()["torrents"], [])

    def test_delete_from_ui_alone_with_magnet_entry_does_not_crash(self) -> None:
        # A magnet-added torrent has no watched .torrent file at all
        # (torrent_file is falsy). clear(delete_from_ui=True) must not raise
        # when it reaches the "unlink the watch file" branch for such an
        # entry, and must not spuriously create a stray file on disk.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, watch = self._make_manager(root, rpc)
            entry_id = "magnet1"
            with manager._lock:
                manager._torrents[entry_id] = {
                    "id": entry_id,
                    "name": "magnet-only",
                    "torrent_file": "",
                    "magnet_uri": "magnet:?xt=urn:btih:deadbeefdeadbeefdeadbeefdeadbeefdeadbeef&dn=x",
                    "download_dir": str(root / "downloads"),
                    "status": "downloading",
                    "message": "",
                    "gid": None,
                    "added_at": "2026-01-01T00:00:00+00:00",
                    "completed_at": None,
                    "total_bytes": 0,
                    "completed_bytes": 0,
                    "progress_percent": 0.0,
                    "files": [],
                    "queue_position": 0,
                    "retry_count": 0,
                    "retry_at": 0.0,
                    "last_error": "",
                }

            result = manager.clear({"delete_from_ui": True, "scope": "all"})

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["cleared"], 1)
            self.assertEqual(manager.snapshot()["torrents"], [])
            self.assertEqual(list(watch.glob("*.torrent")), [])

    def test_clear_completed_scope_leaves_non_targeted_watch_file_untouched(self) -> None:
        # Scope isolation: clearing only "completed" entries with
        # delete_from_ui must not unlink the watched file of an entry that
        # was never selected as a clear target.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, watch = self._make_manager(root, rpc)
            _write_torrent(watch, "done")
            _write_torrent(watch, "active")
            manager._tick()
            self._mark_complete(manager, "done")
            manager._tick()

            result = manager.clear({"delete_from_ui": True, "scope": "completed"})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["cleared"], 1)

            remaining_files = {p.stem for p in watch.glob("*.torrent")}
            self.assertEqual(remaining_files, {"active"})

            manager._tick()
            remaining_names = [e["name"] for e in manager.snapshot()["torrents"]]
            self.assertEqual(remaining_names, ["active"])

    def test_delete_torrent_file_alone_without_ui_removal_does_not_duplicate_row_on_tick(self) -> None:
        # The inverse combination (delete_torrent_file without
        # delete_from_ui): the row must stay visible in the UI, and having
        # already unlinked its own watched file must not cause the scan to
        # either resurrect it as a second row or drop the still-present row.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, watch = self._make_manager(root, rpc)
            _write_torrent(watch, "done")
            manager._tick()
            self._mark_complete(manager, "done")
            manager._tick()

            result = manager.clear({"delete_torrent_file": True, "scope": "completed"})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["cleared"], 1)
            self.assertEqual(list(watch.glob("*.torrent")), [])

            by_name = {e["name"]: e for e in manager.snapshot()["torrents"]}
            self.assertIn("done", by_name)

            manager._tick()
            names_after_tick = [e["name"] for e in manager.snapshot()["torrents"]]
            self.assertEqual(names_after_tick.count("done"), 1)

    def test_unexpected_scope_value_falls_back_to_completed_and_still_unlinks(self) -> None:
        # Malformed input: an unrecognized scope string silently falls back
        # to "completed" (see CLEAR_SCOPES gate) rather than erroring. The
        # delete_from_ui unlink-fix must still apply under that fallback.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, watch = self._make_manager(root, rpc)
            _write_torrent(watch, "done")
            manager._tick()
            self._mark_complete(manager, "done")
            manager._tick()

            result = manager.clear({"delete_from_ui": True, "scope": "not-a-real-scope"})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["cleared"], 1)
            self.assertEqual(list(watch.glob("*.torrent")), [])

            manager._tick()
            self.assertEqual(manager.snapshot()["torrents"], [])

    def test_truthy_non_bool_delete_from_ui_value_still_triggers_unlink(self) -> None:
        # Malformed/loosely-typed input: callers may send a JSON string like
        # "true" rather than a real boolean. clear() coerces with bool(...),
        # so any truthy value must still take the delete_from_ui unlink path.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, watch = self._make_manager(root, rpc)
            _write_torrent(watch, "done")
            manager._tick()
            self._mark_complete(manager, "done")
            manager._tick()

            result = manager.clear({"delete_from_ui": "true", "scope": "completed"})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["cleared"], 1)
            self.assertEqual(list(watch.glob("*.torrent")), [])

            manager._tick()
            self.assertEqual(manager.snapshot()["torrents"], [])

    def test_repeated_clear_calls_are_idempotent_after_watch_file_already_unlinked(self) -> None:
        # Calling clear() a second time once the target set is already empty
        # must not raise (e.g. re-unlinking an already-removed path) and
        # must not resurrect anything on a subsequent tick.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, watch = self._make_manager(root, rpc)
            _write_torrent(watch, "done")
            manager._tick()
            self._mark_complete(manager, "done")
            manager._tick()

            first = manager.clear({"delete_from_ui": True, "scope": "completed"})
            self.assertEqual(first["cleared"], 1)

            second = manager.clear({"delete_from_ui": True, "scope": "completed"})
            self.assertEqual(second["status"], "ok")
            self.assertEqual(second["cleared"], 0)

            manager._tick()
            self.assertEqual(manager.snapshot()["torrents"], [])


if __name__ == "__main__":
    unittest.main()
