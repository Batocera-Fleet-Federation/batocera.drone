import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import app.transfer.aria2_runtime as aria2_runtime
import app.transfer.torrent_manager as torrent_manager
from app.common.settings import Settings
from app.transfer.aria2_runtime import Aria2RpcError, _asset_for_machine, _extract_aria2c_from_zip, install_aria2
from app.transfer.torrent_manager import (
    TorrentManager,
    _normalize_torrent_settings,
    default_torrent_directory,
    effective_download_directory,
)


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "local-test",
        "LOG_DIR": str(root / "logs"),
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class FakeRpc:
    def __init__(self):
        self.calls = []
        self._gid_counter = 0
        self.statuses = {}

    def call(self, method, params=None, timeout=None):
        params = params or []
        self.calls.append((method, params))
        if method == "aria2.addTorrent":
            self._gid_counter += 1
            gid = f"gid{self._gid_counter}"
            paused = params[2].get("pause") == "true"
            self.statuses[gid] = {
                "gid": gid,
                "status": "paused" if paused else "active",
                "totalLength": "0",
                "completedLength": "0",
                "downloadSpeed": "0",
            }
            return gid
        if method == "aria2.addUri":
            self._gid_counter += 1
            gid = f"gid{self._gid_counter}"
            paused = params[1].get("pause") == "true"
            self.statuses[gid] = {
                "gid": gid,
                "status": "paused" if paused else "active",
                "totalLength": "0",
                "completedLength": "0",
                "downloadSpeed": "0",
            }
            return gid
        if method == "aria2.tellStatus":
            gid = params[0]
            if gid not in self.statuses:
                raise Aria2RpcError(f"GID {gid} is not found")
            return self.statuses[gid]
        if method == "aria2.unpause":
            gid = params[0]
            if gid in self.statuses:
                self.statuses[gid]["status"] = "active"
            return gid
        return "OK"

    def method_calls(self, name):
        return [params for method, params in self.calls if method == name]


class FakeDaemon:
    def __init__(self, rpc):
        self.rpc = rpc
        self.binary_path = "/fake/aria2c"
        self.last_error = ""

    @property
    def running(self):
        return True


def _write_torrent(directory: Path, name: str) -> Path:
    path = directory / f"{name}.torrent"
    path.write_bytes(b"d8:announce0:e")
    return path


class TorrentSettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            config = _normalize_torrent_settings({}, settings)
            self.assertEqual(config["directory"], str(default_torrent_directory(settings)))
            # The default lives where the Drone app is physically installed
            # (<install root>/torrents), not under the userdata root.
            install_root = Path(torrent_manager.__file__).resolve().parents[2]
            self.assertEqual(config["directory"], str(install_root / "torrents"))
            self.assertEqual(config["seed_time"], 60)
            self.assertEqual(config["seed_ratio"], 1.0)
            self.assertEqual(config["bt_stop_timeout"], 0)
            self.assertEqual(config["file_allocation"], "prealloc")
            self.assertEqual(config["max_concurrent_downloads"], 3)

    def test_clamps_and_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            config = _normalize_torrent_settings(
                {
                    "directory": "  ",
                    "seed_time": -5,
                    "seed_ratio": -1,
                    "bt_stop_timeout": "abc",
                    "file_allocation": "bogus",
                    "max_concurrent_downloads": 99,
                },
                settings,
            )
            self.assertEqual(config["directory"], str(default_torrent_directory(settings)))
            self.assertEqual(config["seed_time"], 0)
            self.assertEqual(config["seed_ratio"], 0.0)
            self.assertEqual(config["bt_stop_timeout"], 0)
            self.assertEqual(config["file_allocation"], "prealloc")
            self.assertEqual(config["max_concurrent_downloads"], 16)

    def test_update_settings_merges_partial_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            config = manager.update_settings({"max_concurrent_downloads": 1})
            self.assertEqual(config["directory"], str(watch))
            self.assertEqual(config["max_concurrent_downloads"], 1)
            self.assertTrue(watch.is_dir())


class TorrentWatchScanTests(unittest.TestCase):
    def test_new_torrent_files_are_registered_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "game")
            (watch / "notes.txt").write_text("ignored", encoding="utf-8")
            with mock.patch.object(torrent_manager, "find_aria2c", return_value=None):
                manager._tick()
                manager._tick()
            snapshot_entries = manager.snapshot()["torrents"]
            self.assertEqual(len(snapshot_entries), 1)
            self.assertEqual(snapshot_entries[0]["status"], "queued")
            self.assertEqual(snapshot_entries[0]["message"], "aria2c is not installed")

    def test_directory_change_keeps_torrent_file_but_download_dir_follows_until_started(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            old_watch = root / "old"
            new_watch = root / "new"
            manager.update_settings({"directory": str(old_watch)})
            _write_torrent(old_watch, "before")
            with mock.patch.object(torrent_manager, "find_aria2c", return_value=None):
                manager._tick()
                manager.update_settings({"directory": str(new_watch)})
                _write_torrent(old_watch, "left-behind")
                _write_torrent(new_watch, "after")
                manager._tick()
            names = sorted(entry["name"] for entry in manager.snapshot()["torrents"])
            self.assertEqual(names, ["after", "before"])
            before = next(e for e in manager.snapshot()["torrents"] if e["name"] == "before")
            # The .torrent file itself never moves...
            self.assertTrue(Path(before["torrent_file"]).is_relative_to(old_watch.resolve()))
            # ...but since aria2c was never installed it never actually
            # started downloading, so its (un-overridden) download location
            # keeps tracking the current watch directory rather than being
            # stuck on a stale snapshot from scan time.
            self.assertEqual(before["download_dir"], str(new_watch))


class TorrentDownloadLocationTests(unittest.TestCase):
    def test_defaults_to_the_watch_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            config = _normalize_torrent_settings({}, settings)
            self.assertEqual(config["download_directory"], "")
            self.assertEqual(effective_download_directory(config), config["directory"])

    def test_override_is_used_for_newly_scanned_torrents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            watch = root / "watch"
            downloads = root / "external-drive" / "roms"
            manager.update_settings({"directory": str(watch), "download_directory": str(downloads)})
            _write_torrent(watch, "game")
            with mock.patch.object(torrent_manager, "find_aria2c", return_value=None):
                manager._tick()
            entry = manager.snapshot()["torrents"][0]
            self.assertEqual(entry["download_dir"], str(downloads))
            # The .torrent file itself still lives in the watched folder --
            # only the downloaded payload goes elsewhere.
            self.assertTrue(Path(entry["torrent_file"]).is_relative_to(watch.resolve()))

    def test_override_create_directory_on_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            downloads = root / "external-drive" / "roms"
            self.assertFalse(downloads.exists())
            manager.update_settings({"download_directory": str(downloads)})
            self.assertTrue(downloads.is_dir())

    def test_clearing_override_reverts_to_watch_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            watch = root / "watch"
            downloads = root / "downloads"
            manager.update_settings({"directory": str(watch), "download_directory": str(downloads)})
            config = manager.update_settings({"download_directory": ""})
            self.assertEqual(config["download_directory"], "")
            self.assertEqual(effective_download_directory(config), str(watch))

    def test_changing_watch_directory_also_moves_the_unoverridden_default(self) -> None:
        # No explicit download_directory set -- the effective default must
        # track wherever `directory` currently points, not a value baked in
        # at some earlier time.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            first_watch = root / "first"
            second_watch = root / "second"
            manager.update_settings({"directory": str(first_watch)})
            config = manager.update_settings({"directory": str(second_watch)})
            self.assertEqual(effective_download_directory(config), str(second_watch))

    def test_snapshot_reports_effective_directory_and_existence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            watch = root / "watch"
            downloads = root / "not-created-yet"
            manager.update_settings({"directory": str(watch)})
            with manager._lock:
                manager._config["download_directory"] = str(downloads)
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["effective_download_directory"], str(downloads))
            self.assertFalse(snapshot["download_directory_exists"])

    def test_aria2_add_uses_effective_download_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = TorrentManager(_build_settings(root), start_worker=False)
            manager._daemon = FakeDaemon(rpc)
            watch = root / "watch"
            downloads = root / "external-drive" / "roms"
            manager.update_settings({"directory": str(watch), "download_directory": str(downloads)})
            _write_torrent(watch, "game")
            manager._tick()
            adds = rpc.method_calls("aria2.addTorrent")
            self.assertEqual(len(adds), 1)
            self.assertEqual(adds[0][2]["dir"], str(downloads))

    def test_changing_location_retargets_queued_not_yet_started_but_not_the_active_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = TorrentManager(_build_settings(root), start_worker=False)
            manager._daemon = FakeDaemon(rpc)
            watch = root / "watch"
            old_downloads = root / "old-downloads"
            manager.update_settings({"directory": str(watch), "download_directory": str(old_downloads), "max_concurrent_downloads": 1})
            _write_torrent(watch, "a")
            _write_torrent(watch, "b")
            manager._tick()
            entries = manager.snapshot()["torrents"]
            # Which of the two ties for the single slot isn't deterministic
            # (added_at has 1s resolution, so a same-tick scan can tiebreak
            # on entry id) -- only the shape (one active, one waiting) matters.
            active = next(e for e in entries if e["status"] == "downloading")
            waiting = next(e for e in entries if e["status"] == "queued")
            self.assertEqual(active["download_dir"], str(old_downloads))
            self.assertEqual(waiting["download_dir"], str(old_downloads))

            with manager._lock:
                waiting_old_gid = manager._torrents[waiting["id"]]["gid"]

            new_downloads = root / "new-downloads"
            manager.update_settings({"download_directory": str(new_downloads)})
            manager._tick()

            entries = manager.snapshot()["torrents"]
            active_after = next(e for e in entries if e["id"] == active["id"])
            waiting_after = next(e for e in entries if e["id"] == waiting["id"])
            # The already-downloading torrent keeps its original location...
            self.assertEqual(active_after["download_dir"], str(old_downloads))
            # ...but the one that hasn't started yet follows the new setting:
            # aria2 does not honor a `dir` change via aria2.changeOption for
            # an already-added BitTorrent download (confirmed live against a
            # real aria2c), so its stale GID is dropped and it's re-added
            # fresh at the new location instead.
            self.assertEqual(waiting_after["download_dir"], str(new_downloads))
            self.assertIn(waiting_old_gid, [params[0] for params in rpc.method_calls("aria2.forceRemove")])
            adds = rpc.method_calls("aria2.addTorrent")
            self.assertEqual(adds[-1][2]["dir"], str(new_downloads))

    def test_changing_location_does_not_retarget_a_paused_torrent_with_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = TorrentManager(_build_settings(root), start_worker=False)
            manager._daemon = FakeDaemon(rpc)
            watch = root / "watch"
            old_downloads = root / "old-downloads"
            manager.update_settings({"directory": str(watch), "download_directory": str(old_downloads)})
            _write_torrent(watch, "a")
            manager._tick()
            entry = manager.snapshot()["torrents"][0]
            self.assertEqual(entry["status"], "downloading")
            with manager._lock:
                gid = manager._torrents[entry["id"]]["gid"]
            # Simulate real progress, then a global pause -- aria2 reports
            # "paused" with bytes already on disk, which maps to our
            # "queued" status but must not be treated as "not started yet".
            rpc.statuses[gid].update({"completedLength": "500", "totalLength": "1000", "status": "paused"})
            manager.pause()
            manager._tick()
            paused_entry = manager.snapshot()["torrents"][0]
            self.assertEqual(paused_entry["status"], "queued")
            self.assertEqual(paused_entry["completed_bytes"], 500)

            new_downloads = root / "new-downloads"
            manager.update_settings({"download_directory": str(new_downloads)})
            manager._tick()

            refreshed = manager.snapshot()["torrents"][0]
            self.assertEqual(refreshed["status"], "queued")
            self.assertEqual(refreshed["download_dir"], str(old_downloads))
            self.assertEqual(rpc.method_calls("aria2.forceRemove"), [])

    def test_force_start_retargets_a_not_yet_started_queued_torrent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = TorrentManager(_build_settings(root), start_worker=False)
            manager._daemon = FakeDaemon(rpc)
            watch = root / "watch"
            old_downloads = root / "old-downloads"
            manager.update_settings({"directory": str(watch), "download_directory": str(old_downloads), "max_concurrent_downloads": 1})
            _write_torrent(watch, "a")
            _write_torrent(watch, "b")
            manager._tick()
            queued = next(e for e in manager.snapshot()["torrents"] if e["status"] == "queued")
            with manager._lock:
                old_gid = manager._torrents[queued["id"]]["gid"]

            new_downloads = root / "new-downloads"
            manager.update_settings({"download_directory": str(new_downloads)})
            result = manager.force_start(queued["id"])
            self.assertEqual(result["status"], "ok")
            # The stale GID (pointed at the old directory) is torn down
            # immediately, rather than left running against the old location.
            self.assertIn(old_gid, [params[0] for params in rpc.method_calls("aria2.forceRemove")])
            with manager._lock:
                self.assertIsNone(manager._torrents[queued["id"]]["gid"])

            # The next tick re-adds it fresh, unpaused, at the new location.
            manager._tick()
            refreshed = next(e for e in manager.snapshot()["torrents"] if e["id"] == queued["id"])
            self.assertEqual(refreshed["download_dir"], str(new_downloads))
            self.assertEqual(refreshed["status"], "downloading")
            adds = rpc.method_calls("aria2.addTorrent")
            self.assertEqual(adds[-1][2]["dir"], str(new_downloads))
            self.assertEqual(adds[-1][2]["pause"], "false")


class TorrentLifecycleTests(unittest.TestCase):
    def _manager(self, root: Path, rpc: FakeRpc) -> TorrentManager:
        manager = TorrentManager(_build_settings(root), start_worker=False)
        manager._daemon = FakeDaemon(rpc)
        return manager

    def test_adds_paused_and_schedules_up_to_concurrency_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 2})
            for name in ("a", "b", "c"):
                _write_torrent(watch, name)
            manager._tick()
            adds = rpc.method_calls("aria2.addTorrent")
            self.assertEqual(len(adds), 3)
            self.assertTrue(all(params[2]["pause"] == "true" for params in adds))
            self.assertEqual(adds[0][2]["seed-time"], "60")
            self.assertEqual(adds[0][2]["file-allocation"], "prealloc")
            self.assertEqual(len(rpc.method_calls("aria2.unpause")), 2)
            statuses = [entry["status"] for entry in manager.snapshot()["torrents"]]
            self.assertEqual(statuses.count("downloading"), 2)
            self.assertEqual(statuses.count("queued"), 1)

    def test_status_mapping_progress_complete_and_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 3})
            for name in ("a", "b", "c"):
                _write_torrent(watch, name)
            manager._tick()
            entries = manager.snapshot()["torrents"]
            gid_by_name = {}
            with manager._lock:
                for entry in manager._torrents.values():
                    gid_by_name[entry["name"]] = entry["gid"]
            rpc.statuses[gid_by_name["a"]] = {
                "gid": gid_by_name["a"],
                "status": "active",
                "totalLength": "1000",
                "completedLength": "250",
                "downloadSpeed": "50",
                "uploadSpeed": "5",
                "connections": "4",
                "numSeeders": "3",
                "bittorrent": {"info": {"name": "Game A (USA)"}},
            }
            rpc.statuses[gid_by_name["b"]] = {
                "gid": gid_by_name["b"],
                "status": "active",
                "totalLength": "1000",
                "completedLength": "1000",
                "downloadSpeed": "0",
                "uploadSpeed": "9",
            }
            rpc.statuses[gid_by_name["c"]] = {
                "gid": gid_by_name["c"],
                "status": "error",
                "totalLength": "0",
                "completedLength": "0",
                "downloadSpeed": "0",
                "errorMessage": "tracker exploded",
            }
            manager._tick()
            by_name = {entry["name"]: entry for entry in manager.snapshot()["torrents"]}
            game_a = by_name["Game A (USA)"]
            self.assertEqual(game_a["status"], "downloading")
            self.assertEqual(game_a["progress_percent"], 25.0)
            self.assertEqual(game_a["download_speed_bps"], 50)
            self.assertEqual(game_a["num_seeders"], 3)
            self.assertEqual(game_a["connections"], 4)
            self.assertEqual(game_a["eta_seconds"], 15)
            self.assertEqual(by_name["b"]["status"], "complete")
            self.assertTrue(by_name["b"]["seeding"])
            self.assertIsNotNone(by_name["b"]["completed_at"])
            self.assertEqual(by_name["c"]["status"], "error")
            self.assertEqual(by_name["c"]["message"], "tracker exploded — automatic retry in 15s")

    def test_errored_torrent_retries_behind_everything_already_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 1})
            for name in ("a", "b", "c"):
                _write_torrent(watch, name)
            manager._tick()

            with manager._lock:
                entries = {entry["name"]: entry for entry in manager._torrents.values()}
                failed_gid = entries["a"]["gid"]
            rpc.statuses[failed_gid].update(
                {
                    "status": "error",
                    "errorMessage": "tracker exploded",
                }
            )

            with mock.patch.object(torrent_manager, "TORRENT_RETRY_BASE_SECONDS", 10), \
                    mock.patch.object(torrent_manager.time, "time", return_value=100):
                manager._tick()
            by_name = {entry["name"]: entry for entry in manager.snapshot()["torrents"]}
            self.assertEqual(by_name["a"]["status"], "error")
            self.assertEqual(by_name["b"]["status"], "downloading")
            self.assertEqual(by_name["c"]["status"], "queued")
            self.assertIn(failed_gid, [params[0] for params in rpc.method_calls("aria2.forceRemove")])

            with mock.patch.object(torrent_manager.time, "time", return_value=111):
                manager._tick()
            by_name = {entry["name"]: entry for entry in manager.snapshot()["torrents"]}
            self.assertEqual(by_name["a"]["status"], "queued")
            self.assertEqual(by_name["c"]["status"], "queued")
            with manager._lock:
                entries = {entry["name"]: entry for entry in manager._torrents.values()}
                self.assertLess(entries["c"]["queue_position"], entries["a"]["queue_position"])
                active_b_gid = entries["b"]["gid"]

            rpc.statuses[active_b_gid].update(
                {
                    "status": "complete",
                    "totalLength": "1",
                    "completedLength": "1",
                }
            )
            with mock.patch.object(torrent_manager.time, "time", return_value=112):
                manager._tick()
            by_name = {entry["name"]: entry for entry in manager.snapshot()["torrents"]}
            self.assertEqual(by_name["c"]["status"], "downloading")
            self.assertEqual(by_name["a"]["status"], "queued")

    def test_unreadable_torrent_retries_after_file_is_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = TorrentManager(_build_settings(root), start_worker=False)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 1})
            first_path = _write_torrent(watch, "a")
            with mock.patch.object(torrent_manager, "find_aria2c", return_value=None):
                manager._tick()

            first_path.unlink()
            manager._daemon = FakeDaemon(rpc)
            with mock.patch.object(torrent_manager.time, "time", return_value=100):
                manager._tick()
            first = manager.snapshot()["torrents"][0]
            self.assertEqual(first["status"], "error")
            self.assertIn("torrent file unreadable", first["message"])

            _write_torrent(watch, "b")
            _write_torrent(watch, "a")
            with mock.patch.object(torrent_manager.time, "time", return_value=101):
                manager._tick()
            by_name = {entry["name"]: entry for entry in manager.snapshot()["torrents"]}
            self.assertEqual(by_name["b"]["status"], "downloading")
            self.assertEqual(by_name["a"]["status"], "error")

            with mock.patch.object(torrent_manager.time, "time", return_value=116):
                manager._tick()
            by_name = {entry["name"]: entry for entry in manager.snapshot()["torrents"]}
            self.assertEqual(by_name["b"]["status"], "downloading")
            self.assertEqual(by_name["a"]["status"], "queued")

    def test_force_start_bypasses_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 1})
            for name in ("a", "b"):
                _write_torrent(watch, name)
            manager._tick()
            queued = [entry for entry in manager.snapshot()["torrents"] if entry["status"] == "queued"]
            self.assertEqual(len(queued), 1)
            result = manager.force_start(queued[0]["id"])
            self.assertEqual(result["status"], "ok")
            statuses = [entry["status"] for entry in manager.snapshot()["torrents"]]
            self.assertEqual(statuses.count("downloading"), 2)
            self.assertEqual(len(rpc.method_calls("aria2.unpause")), 2)

    def test_force_start_readds_errored_torrent_unpaused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 1})
            _write_torrent(watch, "a")
            manager._tick()
            entry = manager.snapshot()["torrents"][0]
            with manager._lock:
                gid = next(iter(manager._torrents.values()))["gid"]
            # A real aria2-reported failure (not cancel(), which now requeues
            # rather than erroring -- see test_cancel_active_requeues_* below)
            # is what actually produces an "error" entry to force-start.
            rpc.statuses[gid] = {
                "gid": gid,
                "status": "error",
                "errorMessage": "simulated failure",
                "totalLength": "0",
                "completedLength": "0",
                "downloadSpeed": "0",
            }
            manager._tick()
            self.assertEqual(manager.snapshot()["torrents"][0]["status"], "error")
            result = manager.force_start(entry["id"])
            self.assertEqual(result["status"], "ok")
            manager._tick()
            adds = rpc.method_calls("aria2.addTorrent")
            self.assertEqual(len(adds), 2)
            self.assertEqual(adds[-1][2]["pause"], "false")
            self.assertEqual(manager.snapshot()["torrents"][0]["status"], "downloading")

    def test_cancel_active_requeues_and_removes_from_aria2(self) -> None:
        # "Cancel" on an active/queued torrent now sends it to the back of
        # the queue (stop + free its slot) instead of marking it a terminal
        # error, so a slow torrent can be bumped without losing progress or
        # needing a manual Force Start to resume.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "a")
            manager._tick()
            entry = manager.snapshot()["torrents"][0]
            self.assertEqual(entry["status"], "downloading")
            result = manager.cancel(entry["id"])
            self.assertEqual(result["status"], "requeued")
            self.assertEqual(len(rpc.method_calls("aria2.forceRemove")), 1)
            self.assertEqual(len(rpc.method_calls("aria2.removeDownloadResult")), 1)
            refreshed = manager.snapshot()["torrents"][0]
            self.assertEqual(refreshed["status"], "queued")
            self.assertEqual(refreshed["message"], "")

            # Requeued (not a terminal error) means it resumes on its own on
            # the very next tick -- no Force Start required -- and since
            # nothing else is queued ahead of it, it re-takes the free slot.
            manager._tick()
            refreshed = manager.snapshot()["torrents"][0]
            self.assertEqual(refreshed["status"], "downloading")
            self.assertEqual(len(rpc.method_calls("aria2.addTorrent")), 2)

    def test_cancel_errored_torrent_requeues_instead_of_not_cancelable(self) -> None:
        # "Send to queue" is also offered on an errored torrent (in addition
        # to Force Start) as a way to retry it without jumping the queue --
        # cancel() must actually requeue an "error" entry, not reject it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 1})
            _write_torrent(watch, "a")
            manager._tick()
            with manager._lock:
                gid = next(iter(manager._torrents.values()))["gid"]
            rpc.statuses[gid] = {
                "gid": gid,
                "status": "error",
                "errorMessage": "simulated failure",
                "totalLength": "0",
                "completedLength": "0",
                "downloadSpeed": "0",
            }
            manager._tick()
            entry = manager.snapshot()["torrents"][0]
            self.assertEqual(entry["status"], "error")

            result = manager.cancel(entry["id"])
            self.assertEqual(result["status"], "requeued")
            refreshed = manager.snapshot()["torrents"][0]
            self.assertEqual(refreshed["status"], "queued")
            self.assertEqual(refreshed["message"], "")

            manager._tick()
            refreshed = manager.snapshot()["torrents"][0]
            self.assertEqual(refreshed["status"], "downloading")

    def test_cancel_sends_torrent_to_back_of_queue(self) -> None:
        # With the concurrency slot already taken by "a", cancelling it while
        # "b" is waiting must land "a" behind "b" -- proving this is a real
        # back-of-queue requeue, not just an in-place status flip.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 1})
            _write_torrent(watch, "a")
            manager._tick()
            _write_torrent(watch, "b")
            manager._tick()
            by_name = {entry["name"]: entry for entry in manager.snapshot()["torrents"]}
            self.assertEqual(by_name["a"]["status"], "downloading")
            self.assertEqual(by_name["b"]["status"], "queued")

            manager.cancel(by_name["a"]["id"])
            manager._tick()
            by_name = {entry["name"]: entry for entry in manager.snapshot()["torrents"]}
            self.assertEqual(by_name["b"]["status"], "downloading")
            self.assertEqual(by_name["a"]["status"], "queued")

    def test_cancel_seeding_torrent_stays_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "a")
            manager._tick()
            gid = rpc.method_calls("aria2.tellStatus") and None
            with manager._lock:
                entry = next(iter(manager._torrents.values()))
                gid = entry["gid"]
            rpc.statuses[gid] = {
                "gid": gid,
                "status": "active",
                "totalLength": "10",
                "completedLength": "10",
                "downloadSpeed": "0",
            }
            manager._tick()
            entry = manager.snapshot()["torrents"][0]
            self.assertEqual(entry["status"], "complete")
            self.assertTrue(entry["seeding"])
            result = manager.cancel(entry["id"])
            self.assertEqual(result["status"], "seeding_stopped")
            refreshed = manager.snapshot()["torrents"][0]
            self.assertEqual(refreshed["status"], "complete")
            self.assertFalse(refreshed["seeding"])
            self.assertEqual(refreshed["message"], "Seeding stopped")

    def test_delete_removes_entry_torrent_file_and_downloaded_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            torrent_path = _write_torrent(watch, "a")
            payload_file = watch / "a.bin"
            payload_file.write_bytes(b"payload")
            manager._tick()
            with manager._lock:
                gid = next(iter(manager._torrents.values()))["gid"]
            rpc.statuses[gid].update(
                {"totalLength": "7", "completedLength": "7", "files": [{"path": str(payload_file)}]}
            )
            manager._tick()
            entry = manager.snapshot()["torrents"][0]
            self.assertEqual(entry["status"], "complete")
            result = manager.delete(entry["id"])
            self.assertEqual(result["status"], "deleted")
            self.assertTrue(result["downloaded_files_removed"])
            self.assertFalse(torrent_path.exists())
            self.assertFalse(payload_file.exists())
            self.assertEqual(manager.snapshot()["torrents"], [])
            self.assertEqual(len(rpc.method_calls("aria2.forceRemove")), 1)
            with mock.patch.object(torrent_manager, "find_aria2c", return_value=None):
                manager._tick()
            self.assertEqual(manager.snapshot()["torrents"], [])

    def test_delete_with_no_known_files_reports_removed_true_vacuously(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "a")
            manager._tick()
            entry = manager.snapshot()["torrents"][0]
            result = manager.delete(entry["id"])
            self.assertEqual(result["status"], "deleted")
            self.assertTrue(result["downloaded_files_removed"])

    def test_restart_restores_registry_and_requeues_inflight_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "a")
            manager._tick()
            self.assertEqual(manager.snapshot()["torrents"][0]["status"], "downloading")
            restarted = TorrentManager(_build_settings(root), start_worker=False)
            entries = restarted.snapshot()["torrents"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["status"], "queued")
            self.assertEqual(restarted.snapshot()["settings"]["directory"], str(watch))
            with restarted._lock:
                self.assertIsNone(next(iter(restarted._torrents.values()))["gid"])

    def test_stale_gid_after_daemon_restart_requeues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "a")
            manager._tick()
            with manager._lock:
                entry = next(iter(manager._torrents.values()))
                stale_gid = entry["gid"]
            del rpc.statuses[stale_gid]
            manager._tick()
            # The stale GID is dropped and the torrent re-added on the same or
            # next tick rather than being stuck downloading forever.
            with manager._lock:
                entry = next(iter(manager._torrents.values()))
                self.assertIn(entry["status"], ("queued", "downloading"))
                if entry["status"] == "downloading":
                    self.assertNotEqual(entry["gid"], stale_gid)


_MAGNET_URI = (
    "magnet:?xt=urn:btih:c12fe1c06bba254a9dc9f519b335aa7c1367a88a&dn=Some+Test+File"
)


class TorrentCompletedNotificationTests(unittest.TestCase):
    """torrent_completed must fire exactly once per torrent, the moment
    completed_at is first set -- never again on later ticks that just
    re-confirm the same complete/seeding status."""

    def _manager(self, root: Path, rpc: FakeRpc) -> TorrentManager:
        manager = TorrentManager(_build_settings(root), start_worker=False)
        manager._daemon = FakeDaemon(rpc)
        return manager

    def test_fires_once_when_transitioning_to_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "game")
            with mock.patch.object(torrent_manager, "_notifications") as fake_notifications:
                manager._tick()  # registers + adds (still active/incomplete)
                with manager._lock:
                    gid = next(iter(manager._torrents.values()))["gid"]
                rpc.statuses[gid] = {
                    "gid": gid, "status": "active", "totalLength": "100", "completedLength": "100", "downloadSpeed": "0",
                }
                manager._tick()  # completes here
                manager._tick()  # must not re-fire
                manager._tick()
            fake_notifications.record_event.assert_called_once()
            self.assertEqual(fake_notifications.record_event.call_args[0][1], "torrent_completed")
            self.assertEqual(manager.snapshot()["torrents"][0]["status"], "complete")

    def test_does_not_fire_while_still_downloading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "game")
            with mock.patch.object(torrent_manager, "_notifications") as fake_notifications:
                manager._tick()
                with manager._lock:
                    gid = next(iter(manager._torrents.values()))["gid"]
                rpc.statuses[gid] = {
                    "gid": gid, "status": "active", "totalLength": "100", "completedLength": "50", "downloadSpeed": "10",
                }
                manager._tick()
            fake_notifications.record_event.assert_not_called()


class TorrentMagnetTests(unittest.TestCase):
    def _manager(self, root: Path, rpc: FakeRpc) -> TorrentManager:
        manager = TorrentManager(_build_settings(root), start_worker=False)
        manager._daemon = FakeDaemon(rpc)
        return manager

    def test_add_magnet_rejects_invalid_uri(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(Path(tmp), FakeRpc())
            with self.assertRaises(ValueError):
                manager.add_magnet("not-a-magnet-link")
            with self.assertRaises(ValueError):
                manager.add_magnet("magnet:?dn=missing-infohash")

    def test_add_magnet_registers_entry_with_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(Path(tmp), FakeRpc())
            result = manager.add_magnet(_MAGNET_URI)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["name"], "Some Test File")
            entries = manager.snapshot()["torrents"]
            self.assertEqual(len(entries), 1)
            self.assertTrue(entries[0]["is_magnet"])
            self.assertEqual(entries[0]["magnet_uri"], _MAGNET_URI)
            self.assertEqual(entries[0]["status"], "queued")

    def test_tick_adds_magnet_via_add_uri_paused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            manager.add_magnet(_MAGNET_URI)
            manager._tick()
            adds = rpc.method_calls("aria2.addUri")
            self.assertEqual(len(adds), 1)
            self.assertEqual(adds[0][0], [_MAGNET_URI])
            self.assertEqual(adds[0][1]["pause"], "true")
            entries = manager.snapshot()["torrents"]
            self.assertIsNotNone(entries[0]["download_dir"])
            with manager._lock:
                gid = next(iter(manager._torrents.values()))["gid"]
            self.assertIsNotNone(gid)
            # aria2.addTorrent must never be called for a magnet-only entry --
            # that path does Path(entry["torrent_file"]).read_bytes(), which
            # would raise TypeError on the empty torrent_file a magnet entry has.
            self.assertEqual(rpc.method_calls("aria2.addTorrent"), [])

    def test_magnet_metadata_followed_by_switches_to_real_gid(self) -> None:
        # Regression test for aria2's magnet-metadata handoff: the GID a
        # magnet link is added under only ever fetches the .torrent metadata
        # itself (a few KB/MB) and then reports "complete" at that tiny size,
        # with a `followedBy` pointing at a brand-new GID that carries the
        # real, much larger content download. Confirmed live against a real
        # aria2c and a real multi-GB magnet link -- without following the
        # handoff, the UI showed the torrent "complete" at the metadata's
        # tiny size (exactly "downloads the wrong/tiny file") while the real
        # download ran to completion completely untracked.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            manager.add_magnet(_MAGNET_URI)
            manager._tick()
            self.assertEqual(manager.snapshot()["torrents"][0]["status"], "downloading")
            with manager._lock:
                metadata_gid = next(iter(manager._torrents.values()))["gid"]

            # aria2 finishes the metadata-only fetch and hands off to a new
            # GID for the real content (real numbers from a live repro).
            rpc.statuses[metadata_gid] = {
                "gid": metadata_gid,
                "status": "complete",
                "totalLength": "1192915",
                "completedLength": "1192915",
                "downloadSpeed": "0",
                "followedBy": ["content-gid"],
            }
            rpc.statuses["content-gid"] = {
                "gid": "content-gid",
                "status": "active",
                "totalLength": "477183500385",
                "completedLength": "34865152",
                "downloadSpeed": "1048576",
                "numSeeders": "1",
                "connections": "44",
                "bittorrent": {"info": {"name": "Law and Order - SVU (1999 - ongoing)"}},
            }

            manager._tick()
            with manager._lock:
                switched_gid = next(iter(manager._torrents.values()))["gid"]
            self.assertEqual(switched_gid, "content-gid")
            # Must not have latched onto the metadata GID's tiny "complete".
            self.assertNotEqual(manager.snapshot()["torrents"][0]["status"], "complete")

            manager._tick()
            refreshed = manager.snapshot()["torrents"][0]
            self.assertEqual(refreshed["status"], "downloading")
            self.assertEqual(refreshed["total_bytes"], 477183500385)
            self.assertEqual(refreshed["completed_bytes"], 34865152)
            self.assertEqual(refreshed["name"], "Law and Order - SVU (1999 - ongoing)")

    def test_restart_restores_magnet_entry(self) -> None:
        """Regression test for the _restore_state() gate that used to require
        torrent_file unconditionally -- a magnet-only entry would previously
        be silently dropped (not just lose its GID) on every Drone restart."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            manager.add_magnet(_MAGNET_URI)
            manager._tick()
            self.assertEqual(manager.snapshot()["torrents"][0]["status"], "downloading")

            # Simulate a Drone restart: a second, independent TorrentManager
            # pointed at the same state DB (same pattern as
            # test_restart_restores_registry_and_requeues_inflight_downloads).
            restarted = TorrentManager(_build_settings(root), start_worker=False)
            entries = restarted.snapshot()["torrents"]
            self.assertEqual(len(entries), 1)
            self.assertTrue(entries[0]["is_magnet"])
            self.assertEqual(entries[0]["magnet_uri"], _MAGNET_URI)
            self.assertEqual(entries[0]["status"], "queued")
            with restarted._lock:
                self.assertIsNone(next(iter(restarted._torrents.values()))["gid"])

    def test_delete_magnet_entry_does_not_crash_on_empty_torrent_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(Path(tmp), FakeRpc())
            added = manager.add_magnet(_MAGNET_URI)
            result = manager.delete(added["id"])
            self.assertEqual(result["status"], "deleted")
            self.assertTrue(result["torrent_file_removed"])
            self.assertEqual(manager.snapshot()["torrents"], [])


class TorrentUploadTests(unittest.TestCase):
    def _manager_with_watch(self, root: Path):
        manager = TorrentManager(_build_settings(root), start_worker=False)
        watch = root / "watch"
        manager.update_settings({"directory": str(watch)})
        return manager, watch

    def test_upload_saves_multiple_files_and_scanner_registers_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._manager_with_watch(root)
            result = manager.save_uploaded_torrents(
                [("one.torrent", b"d1:ae"), ("two.torrent", b"d1:be")]
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(sorted(result["saved"]), ["one.torrent", "two.torrent"])
            self.assertEqual(result["errors"], [])
            self.assertTrue((watch / "one.torrent").is_file())
            with mock.patch.object(torrent_manager, "find_aria2c", return_value=None):
                manager._tick()
            names = sorted(entry["name"] for entry in manager.snapshot()["torrents"])
            self.assertEqual(names, ["one", "two"])

    def test_upload_sanitizes_traversal_and_rejects_bad_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._manager_with_watch(root)
            result = manager.save_uploaded_torrents(
                [
                    ("../../evil.torrent", b"data"),
                    ("notes.txt", b"data"),
                    ("empty.torrent", b""),
                    (".torrent", b"data"),
                ]
            )
            self.assertEqual(result["saved"], ["evil.torrent"])
            self.assertTrue((watch / "evil.torrent").is_file())
            self.assertFalse((root / "evil.torrent").exists())
            self.assertEqual(len(result["errors"]), 3)

    def test_upload_collision_gets_numbered_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._manager_with_watch(root)
            (watch / "game.torrent").write_bytes(b"existing")
            result = manager.save_uploaded_torrents([("game.torrent", b"new")])
            self.assertEqual(result["saved"], ["game (2).torrent"])
            self.assertEqual((watch / "game.torrent").read_bytes(), b"existing")
            self.assertEqual((watch / "game (2).torrent").read_bytes(), b"new")


class MultipartParserTests(unittest.TestCase):
    def test_parses_multiple_binary_file_parts_exactly(self) -> None:
        from app.web.handlers_torrents import _parse_multipart_files

        boundary = "----WebKitFormBoundaryabc123"
        first = b"d1:a3:\r\ne"  # embedded CRLF must survive byte-exact
        second = b"binary\r\n"  # trailing CRLF inside the file must survive
        body = (
            f"--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="torrents"; filename="one.torrent"\r\n'
            + b"Content-Type: application/x-bittorrent\r\n\r\n"
            + first
            + f"\r\n--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="torrents"; filename="two.torrent"\r\n\r\n'
            + second
            + f"\r\n--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="not_a_file"\r\n\r\n'
            + b"just a field"
            + f"\r\n--{boundary}--\r\n".encode()
        )
        files = _parse_multipart_files(body, boundary)
        self.assertEqual([(name, data) for name, data in files], [("one.torrent", first), ("two.torrent", second)])


class TorrentBrowseTests(unittest.TestCase):
    def test_browse_roots_and_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            (root / "roms" / "nes").mkdir(parents=True)
            (root / ".hidden").mkdir()
            (root / "file.txt").write_text("x", encoding="utf-8")
            listing = manager.browse_directories("")
            self.assertEqual(listing["path"], "")
            self.assertIn(str(root.resolve()), [d["path"] for d in listing["dirs"]])
            listing = manager.browse_directories(str(root))
            names = [d["name"] for d in listing["dirs"]]
            self.assertIn("roms", names)
            self.assertNotIn(".hidden", names)
            self.assertNotIn("file.txt", names)
            sub = manager.browse_directories(str(root / "roms"))
            self.assertEqual([d["name"] for d in sub["dirs"]], ["nes"])
            self.assertEqual(sub["parent"], str(root.resolve()))

    def test_browse_rejects_paths_outside_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = TorrentManager(_build_settings(Path(tmp)), start_worker=False)
            with self.assertRaises(ValueError):
                manager.browse_directories("/etc")
            with self.assertRaises(ValueError):
                manager.browse_directories(str(Path(tmp) / "missing"))


class TorrentFileManagementTests(unittest.TestCase):
    def _completed_manager(self, root: Path, rpc: FakeRpc, files_by_name: dict):
        """Build a manager with one .torrent per name in ``files_by_name``,
        tick it to completion, and stamp aria2's reported ``files`` for each."""
        manager = TorrentManager(_build_settings(root), start_worker=False)
        manager._daemon = FakeDaemon(rpc)
        watch = root / "watch"
        manager.update_settings({"directory": str(watch)})
        for name in files_by_name:
            _write_torrent(watch, name)
        manager._tick()
        with manager._lock:
            gid_by_name = {entry["name"]: entry["gid"] for entry in manager._torrents.values()}
        for name, paths in files_by_name.items():
            total = 0
            for p in paths:
                try:
                    total += len(Path(p).read_bytes())
                except OSError:
                    pass
            rpc.statuses[gid_by_name[name]].update(
                {
                    "totalLength": str(total),
                    "completedLength": str(total),
                    "files": [{"path": str(p)} for p in paths],
                }
            )
        manager._tick()
        return manager, watch

    def test_list_files_not_applicable_before_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            manager._daemon = FakeDaemon(FakeRpc())
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "a")
            manager._tick()
            entry = manager.snapshot()["torrents"][0]
            result = manager.list_files(entry["id"])
            self.assertEqual(result["status"], "not_applicable")

    def test_list_files_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = TorrentManager(_build_settings(Path(tmp)), start_worker=False)
            self.assertEqual(manager.list_files("missing")["status"], "not_found")

    def test_list_files_returns_known_files_with_size_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            payload = watch / "a.bin"
            payload.write_bytes(b"hello")
            manager, _ = self._completed_manager(root, FakeRpc(), {"a": [payload]})
            entry = manager.snapshot()["torrents"][0]
            self.assertEqual(entry["status"], "complete")
            result = manager.list_files(entry["id"])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(result["files"]), 1)
            self.assertEqual(result["files"][0]["size"], 5)
            self.assertTrue(result["files"][0]["exists"])

    def test_list_files_falls_back_to_walking_a_multi_file_subfolder(self) -> None:
        # Entries that predate the persisted `files` field (or where aria2
        # never reported one) used to guess a single path of `download_dir /
        # name` and surface it as-is -- for a multi-file torrent that guess is
        # a *directory*, not a file, which showed up in the UI as "one thing
        # that isn't a file". The fallback must walk into it instead.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            download_dir = root / "downloads"
            pack_dir = download_dir / "pack"
            pack_dir.mkdir(parents=True)
            (pack_dir / "one.bin").write_bytes(b"one")
            (pack_dir / "two.bin").write_bytes(b"twotwo")
            manager = TorrentManager(_build_settings(root), start_worker=False)
            with manager._lock:
                manager._torrents["e1"] = {
                    "id": "e1",
                    "name": "pack",
                    "torrent_file": str(root / "pack.torrent"),
                    "download_dir": str(download_dir),
                    "status": "complete",
                    "message": "",
                    "added_at": "2026-01-01T00:00:00+00:00",
                    "completed_at": "2026-01-01T00:01:00+00:00",
                    "total_bytes": 9,
                    "completed_bytes": 9,
                    "progress_percent": 100.0,
                    "files": [],
                    "gid": None,
                    "force_started": False,
                    "seeding": False,
                    "download_speed_bps": 0,
                    "upload_speed_bps": 0,
                    "num_seeders": 0,
                    "connections": 0,
                    "eta_seconds": None,
                }
            result = manager.list_files("e1")
            self.assertEqual(result["status"], "ok")
            names = sorted(f["name"] for f in result["files"])
            self.assertEqual(names, ["one.bin", "two.bin"])
            self.assertTrue(all(f["exists"] for f in result["files"]))

    def test_move_files_not_applicable_before_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            manager._daemon = FakeDaemon(FakeRpc())
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "a")
            manager._tick()
            entry = manager.snapshot()["torrents"][0]
            result = manager.move_files(entry["id"], ["/anything"], str(root / "dest"), cleanup=False)
            self.assertEqual(result["status"], "not_applicable")

    def test_move_files_rejects_unknown_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            payload = watch / "a.bin"
            payload.write_bytes(b"hello")
            manager, _ = self._completed_manager(root, FakeRpc(), {"a": [payload]})
            entry = manager.snapshot()["torrents"][0]
            result = manager.move_files(entry["id"], [str(watch / "not-mine.bin")], str(root / "dest"), cleanup=False)
            self.assertEqual(result["status"], "no_files_selected")
            self.assertTrue(payload.exists())

    def test_move_files_rejects_destination_outside_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            payload = watch / "a.bin"
            payload.write_bytes(b"hello")
            manager, _ = self._completed_manager(root, FakeRpc(), {"a": [payload]})
            entry = manager.snapshot()["torrents"][0]
            result = manager.move_files(entry["id"], [str(payload)], "/etc/somewhere", cleanup=False)
            self.assertEqual(result["status"], "invalid_destination")
            self.assertTrue(payload.exists())

    def test_move_files_single_file_in_shared_dir_only_touches_selected_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            payload_a = watch / "a.bin"
            payload_b = watch / "b.bin"
            payload_a.write_bytes(b"aaa")
            payload_b.write_bytes(b"bbb")
            manager, _ = self._completed_manager(root, FakeRpc(), {"a": [payload_a], "b": [payload_b]})
            entry_a = next(e for e in manager.snapshot()["torrents"] if e["name"] == "a")
            dest = root / "moved"
            result = manager.move_files(entry_a["id"], [str(payload_a)], str(dest), cleanup=True)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["cleanup_performed"])
            self.assertTrue((dest / "a.bin").exists())
            self.assertFalse(payload_a.exists())
            # The shared download dir (and the sibling torrent's own file) is
            # never wholesale-deleted -- only the specifically known file(s).
            self.assertTrue(payload_b.exists())
            self.assertIn(str(dest.resolve()), manager.snapshot()["recent_move_locations"])

    def test_move_files_cleanup_removes_dedicated_subfolder_when_all_moved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            subfolder = watch / "pack"
            subfolder.mkdir(parents=True)
            file1 = subfolder / "one.bin"
            file2 = subfolder / "two.bin"
            file1.write_bytes(b"one")
            file2.write_bytes(b"two")
            manager, _ = self._completed_manager(root, FakeRpc(), {"pack": [file1, file2]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"
            result = manager.move_files(entry["id"], [str(file1), str(file2)], str(dest), cleanup=True)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["cleanup_performed"])
            self.assertTrue((dest / "one.bin").exists())
            self.assertTrue((dest / "two.bin").exists())
            self.assertFalse(subfolder.exists())

    def test_move_files_cleanup_success_removes_torrent_from_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            watch = root / "watch"
            subfolder = watch / "pack"
            subfolder.mkdir(parents=True)
            file1 = subfolder / "one.bin"
            file1.write_bytes(b"one")
            manager, _ = self._completed_manager(root, rpc, {"pack": [file1]})
            entry = manager.snapshot()["torrents"][0]
            with manager._lock:
                gid = manager._torrents[entry["id"]]["gid"]
            torrent_file = Path(entry["torrent_file"])
            self.assertTrue(torrent_file.exists())
            dest = root / "moved"
            result = manager.move_files(entry["id"], [str(file1)], str(dest), cleanup=True)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["cleanup_performed"])
            self.assertTrue(result["removed_from_list"])
            self.assertEqual(manager.snapshot()["torrents"], [])
            self.assertFalse(torrent_file.exists())
            self.assertIn(gid, [params[0] for params in rpc.method_calls("aria2.forceRemove")])

    def test_move_files_cleanup_failure_keeps_torrent_in_list(self) -> None:
        # cleanup only fires on a fully-successful move; a partial failure
        # must never remove the torrent out from under files that are still
        # sitting where they started.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            subfolder = watch / "pack"
            subfolder.mkdir(parents=True)
            file1 = subfolder / "one.bin"
            file1.write_bytes(b"one")
            missing = subfolder / "missing.bin"
            manager, _ = self._completed_manager(root, FakeRpc(), {"pack": [file1, missing]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"
            result = manager.move_files(entry["id"], [str(file1), str(missing)], str(dest), cleanup=True)
            self.assertEqual(result["status"], "partial")
            self.assertFalse(result["cleanup_performed"])
            self.assertFalse(result["removed_from_list"])
            self.assertEqual(len(manager.snapshot()["torrents"]), 1)

    def test_move_files_partial_failure_skips_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            payload = watch / "a.bin"
            payload.write_bytes(b"data")
            missing = watch / "missing.bin"  # reported by aria2 but no longer on disk
            manager, _ = self._completed_manager(root, FakeRpc(), {"a": [payload, missing]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"
            result = manager.move_files(entry["id"], [str(payload), str(missing)], str(dest), cleanup=True)
            self.assertEqual(result["status"], "partial")
            self.assertFalse(result["cleanup_performed"])
            self.assertTrue((dest / "a.bin").exists())
            self.assertEqual(len(result["errors"]), 1)

    def test_recent_move_locations_dedupes_and_orders_most_recent_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            payload_a = watch / "a.bin"
            payload_b = watch / "b.bin"
            payload_a.write_bytes(b"aaa")
            payload_b.write_bytes(b"bbb")
            manager, _ = self._completed_manager(root, FakeRpc(), {"a": [payload_a], "b": [payload_b]})
            entries = {e["name"]: e for e in manager.snapshot()["torrents"]}
            dest1 = root / "one"
            dest2 = root / "two"
            manager.move_files(entries["a"]["id"], [str(payload_a)], str(dest1), cleanup=False)
            manager.move_files(entries["b"]["id"], [str(payload_b)], str(dest2), cleanup=False)
            recent = manager.snapshot()["recent_move_locations"]
            self.assertEqual(recent[:2], [str(dest2.resolve()), str(dest1.resolve())])


class TorrentQueueControlTests(unittest.TestCase):
    def test_pause_sets_flag_pauses_aria2_and_blocks_scheduling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = TorrentManager(_build_settings(root), start_worker=False)
            manager._daemon = FakeDaemon(rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            snapshot = manager.pause()
            self.assertTrue(snapshot["paused"])
            self.assertEqual(len(rpc.method_calls("aria2.pauseAll")), 1)
            _write_torrent(watch, "a")
            manager._tick()
            statuses = [entry["status"] for entry in manager.snapshot()["torrents"]]
            self.assertEqual(statuses, ["queued"])
            self.assertEqual(len(rpc.method_calls("aria2.unpause")), 0)

    def test_resume_clears_flag_and_unpauses_aria2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = TorrentManager(_build_settings(root), start_worker=False)
            manager._daemon = FakeDaemon(rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            manager.pause()
            snapshot = manager.resume()
            self.assertFalse(snapshot["paused"])
            self.assertEqual(len(rpc.method_calls("aria2.unpauseAll")), 1)
            _write_torrent(watch, "a")
            manager._tick()
            statuses = [entry["status"] for entry in manager.snapshot()["torrents"]]
            self.assertEqual(statuses, ["downloading"])

    def test_pause_persists_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            manager.pause()
            restarted = TorrentManager(_build_settings(root), start_worker=False)
            self.assertTrue(restarted.snapshot()["paused"])


class TorrentClearTests(unittest.TestCase):
    def _two_torrents(self, root: Path, rpc: FakeRpc):
        manager = TorrentManager(_build_settings(root), start_worker=False)
        manager._daemon = FakeDaemon(rpc)
        watch = root / "watch"
        manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 2})
        done_payload = watch / "done.bin"
        done_payload.write_bytes(b"done")
        _write_torrent(watch, "done")
        _write_torrent(watch, "active")
        manager._tick()
        with manager._lock:
            gid_by_name = {entry["name"]: entry["gid"] for entry in manager._torrents.values()}
        rpc.statuses[gid_by_name["done"]].update(
            {"totalLength": "4", "completedLength": "4", "files": [{"path": str(done_payload)}]}
        )
        manager._tick()
        return manager, watch, done_payload

    def test_clear_requires_at_least_one_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = TorrentManager(_build_settings(Path(tmp)), start_worker=False)
            self.assertEqual(manager.clear({})["status"], "no_action_selected")

    def test_clear_completed_scope_only_touches_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch, done_payload = self._two_torrents(root, FakeRpc())
            by_name = {e["name"]: e for e in manager.snapshot()["torrents"]}
            self.assertEqual(by_name["done"]["status"], "complete")
            self.assertEqual(by_name["active"]["status"], "downloading")
            result = manager.clear({"delete_from_ui": True, "scope": "completed"})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["cleared"], 1)
            remaining = [e["name"] for e in manager.snapshot()["torrents"]]
            self.assertEqual(remaining, ["active"])

    def test_clear_all_scope_with_all_flags_removes_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, watch, done_payload = self._two_torrents(root, rpc)
            result = manager.clear(
                {
                    "delete_from_ui": True,
                    "delete_torrent_file": True,
                    "delete_downloaded_files": True,
                    "scope": "all",
                }
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["cleared"], 2)
            self.assertEqual(manager.snapshot()["torrents"], [])
            self.assertFalse(done_payload.exists())
            self.assertEqual(list(watch.glob("*.torrent")), [])
            self.assertEqual(len(rpc.method_calls("aria2.forceRemove")), 2)

    def test_clear_delete_downloaded_files_without_ui_removal_marks_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch, done_payload = self._two_torrents(root, FakeRpc())
            result = manager.clear({"delete_downloaded_files": True, "scope": "completed"})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["cleared"], 1)
            self.assertFalse(done_payload.exists())
            by_name = {e["name"]: e for e in manager.snapshot()["torrents"]}
            self.assertIn("done", by_name)
            self.assertEqual(by_name["done"]["message"], "Downloaded files removed")


class TorrentDisplaySortTests(unittest.TestCase):
    def test_snapshot_orders_downloading_first_then_queued_error_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = TorrentManager(_build_settings(Path(tmp)), start_worker=False)
            base = {
                "torrent_file": "",
                "download_dir": "",
                "message": "",
                "completed_at": None,
                "total_bytes": 0,
                "completed_bytes": 0,
                "progress_percent": 0.0,
                "files": [],
                "gid": None,
                "force_started": False,
                "seeding": False,
                "download_speed_bps": 0,
                "upload_speed_bps": 0,
                "num_seeders": 0,
                "connections": 0,
                "eta_seconds": None,
            }
            with manager._lock:
                for order, (entry_id, status) in enumerate(
                    [("c1", "complete"), ("q1", "queued"), ("e1", "error"), ("d1", "downloading")]
                ):
                    manager._torrents[entry_id] = {
                        **base,
                        "id": entry_id,
                        "name": entry_id,
                        "status": status,
                        "added_at": f"2026-01-0{order + 1}T00:00:00+00:00",
                    }
            names = [entry["name"] for entry in manager.snapshot()["torrents"]]
            self.assertEqual(names, ["d1", "q1", "e1", "c1"])


class Aria2RuntimeTests(unittest.TestCase):
    def test_asset_mapping(self) -> None:
        self.assertIn("x86_64", _asset_for_machine("x86_64"))
        self.assertIn("aarch64", _asset_for_machine("aarch64"))
        self.assertIn("armv7", _asset_for_machine("armv7l"))
        self.assertIn("musleabi_static", _asset_for_machine("armv6l"))
        with self.assertRaises(ValueError):
            _asset_for_machine("riscv64")

    def test_extract_requires_aria2c_member(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("README.md", "nope")
        with self.assertRaises(ValueError):
            _extract_aria2c_from_zip(buffer.getvalue())

    def test_install_downloads_extracts_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("aria2-x86_64-linux-musl_static/aria2c", b"#!/bin/sh\necho aria2 version 1.37.0\n")
            with mock.patch.object(aria2_runtime, "_download_bytes", return_value=buffer.getvalue()) as download, \
                    mock.patch.object(aria2_runtime, "aria2c_version", return_value="1.37.0"), \
                    mock.patch.object(aria2_runtime.platform, "machine", return_value="x86_64"):
                result = install_aria2(settings)
            self.assertEqual(result["status"], "installed")
            self.assertEqual(result["version"], "1.37.0")
            installed = Path(result["path"])
            self.assertTrue(installed.is_file())
            self.assertTrue(installed.stat().st_mode & 0o111)
            self.assertIn("x86_64-linux-musl_static.zip", download.call_args[0][0])

    def test_install_rejects_binary_that_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("aria2c", b"not a real binary")
            with mock.patch.object(aria2_runtime, "_download_bytes", return_value=buffer.getvalue()), \
                    mock.patch.object(aria2_runtime, "aria2c_version", return_value=None), \
                    mock.patch.object(aria2_runtime.platform, "machine", return_value="x86_64"):
                with self.assertRaises(ValueError):
                    install_aria2(settings)
            self.assertFalse(aria2_runtime.managed_aria2c_path(settings).exists())


if __name__ == "__main__":
    unittest.main()
