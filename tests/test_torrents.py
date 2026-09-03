import contextlib
import io
import shutil
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
        self.timeouts = []
        self._gid_counter = 0
        self.statuses = {}
        # When set, the next aria2.addTorrent/addUri call raises this instead
        # of succeeding (left in place across calls until a test clears it,
        # so a whole retry sequence can be modeled).
        self.add_error = None
        # Pre-populated aria2.tellActive/tellWaiting responses, for testing
        # the "InfoHash already registered" recovery path.
        self.active = []
        self.waiting = []
        # When set, aria2.unpause raises this instead of succeeding -- for
        # testing Phase D's handling of a real aria2 unpause failure.
        self.unpause_error = None
        # When set, the next aria2.forceRemove/removeDownloadResult call
        # raises this instead of succeeding -- for testing the pending-
        # removal retry path (aria2 too busy/unresponsive to confirm a
        # removal right away, confirmed live).
        self.remove_error = None

    def call(self, method, params=None, timeout=None):
        params = params or []
        self.calls.append((method, params))
        self.timeouts.append((method, timeout))
        if method == "aria2.addTorrent":
            if self.add_error:
                raise Aria2RpcError(self.add_error)
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
            if self.add_error:
                raise Aria2RpcError(self.add_error)
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
        if method == "aria2.tellActive":
            return self.active
        if method == "aria2.tellWaiting":
            return self.waiting
        if method == "aria2.unpause":
            if self.unpause_error:
                raise Aria2RpcError(self.unpause_error)
            gid = params[0]
            if gid in self.statuses:
                self.statuses[gid]["status"] = "active"
            return gid
        if method in ("aria2.forceRemove", "aria2.removeDownloadResult"):
            gid = params[0] if params else None
            if self.remove_error:
                raise Aria2RpcError(self.remove_error)
            if gid not in self.statuses:
                raise Aria2RpcError(f"GID#{gid} is not found in the queue.")
            return "OK"
        return "OK"

    def method_calls(self, name):
        return [params for method, params in self.calls if method == name]


class FakeDaemon:
    def __init__(self, rpc):
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
            self.assertFalse(config["vpn_required"])

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
                    "vpn_required": "true",
                },
                settings,
            )
            self.assertEqual(config["directory"], str(default_torrent_directory(settings)))
            self.assertEqual(config["seed_time"], 0)
            self.assertEqual(config["seed_ratio"], 0.0)
            self.assertEqual(config["bt_stop_timeout"], 0)
            self.assertEqual(config["file_allocation"], "prealloc")
            self.assertEqual(config["max_concurrent_downloads"], 16)
            self.assertTrue(config["vpn_required"])

    def test_vpn_required_setting_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            manager.update_settings({"vpn_required": True})
            restarted = TorrentManager(_build_settings(root), start_worker=False)
            self.assertTrue(restarted.snapshot()["settings"]["vpn_required"])

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

    def test_restore_state_resets_a_persisted_custom_directory_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            manager = TorrentManager(settings, start_worker=False)
            watch = root / "custom-watch"
            manager.update_settings({"directory": str(watch)})
            self.assertEqual(manager._config["directory"], str(watch))

            restarted = TorrentManager(_build_settings(root), start_worker=False)
            self.assertEqual(restarted._config["directory"], str(default_torrent_directory(settings)))


class TorrentVpnRequirementTests(unittest.TestCase):
    def test_vpn_down_stops_daemon_and_keeps_torrents_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = TorrentManager(_build_settings(root), start_worker=False)
            daemon = FakeDaemon(rpc)
            manager._daemon = daemon
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "blocked")
            with mock.patch.object(torrent_manager._vpn, "tunnel_is_up", return_value=False):
                manager.update_settings({"vpn_required": True})
                manager._tick()
                snapshot = manager.snapshot()
            self.assertTrue(daemon.stopped)
            self.assertEqual(rpc.calls, [])
            self.assertTrue(snapshot["vpn_required"])
            self.assertFalse(snapshot["vpn_ready"])
            self.assertEqual(snapshot["torrents"][0]["status"], "queued")
            self.assertEqual(snapshot["torrents"][0]["message"], "Waiting for VPN connection")

    def test_vpn_up_launches_daemon_bound_to_tun0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch), "vpn_required": True})
            _write_torrent(watch, "protected")
            fake_rpc = FakeRpc()
            fake_daemon = mock.Mock()
            fake_daemon.binary_path = "/fake/aria2c"
            fake_daemon.bind_interface = "tun0"
            fake_daemon.running = False
            fake_daemon.start.return_value = True
            fake_daemon.rpc = fake_rpc
            with mock.patch.object(torrent_manager._vpn, "tunnel_is_up", return_value=True), \
                    mock.patch.object(torrent_manager, "find_aria2c", return_value={"path": "/fake/aria2c"}), \
                    mock.patch.object(torrent_manager, "Aria2Daemon", return_value=fake_daemon) as daemon_cls:
                manager._tick()
            self.assertEqual(daemon_cls.call_args.kwargs["bind_interface"], "tun0")
            self.assertEqual(len(fake_rpc.method_calls("aria2.addTorrent")), 1)

    def test_force_start_cannot_bypass_missing_vpn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = TorrentManager(_build_settings(root), start_worker=False)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch), "vpn_required": True})
            _write_torrent(watch, "blocked")
            with mock.patch.object(torrent_manager._vpn, "tunnel_is_up", return_value=False):
                manager._tick()
                entry = manager.snapshot()["torrents"][0]
                result = manager.force_start(entry["id"])
            self.assertEqual(result["status"], "vpn_required")


class Aria2VpnBindingTests(unittest.TestCase):
    def test_daemon_command_binds_to_tun0_and_disables_ipv6_and_lpd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            process = mock.Mock()
            process.pid = 123
            process.poll.return_value = None
            rpc = mock.Mock()
            rpc.call.return_value = {"version": "test"}
            with mock.patch.object(aria2_runtime.subprocess, "Popen", return_value=process) as popen, \
                    mock.patch.object(aria2_runtime, "Aria2Rpc", return_value=rpc):
                daemon = aria2_runtime.Aria2Daemon("/fake/aria2c", Path(tmp), bind_interface="tun0")
                self.assertTrue(daemon.start())
            args = popen.call_args.args[0]
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertIn("--interface=tun0", args)
            self.assertIn("--disable-ipv6=true", args)
            self.assertIn("--bt-enable-lpd=false", args)

    def test_stop_terminates_the_entire_appimage_process_group(self) -> None:
        process = mock.Mock()
        process.pid = 456
        process.poll.return_value = None
        daemon = aria2_runtime.Aria2Daemon("/fake/aria2c", Path("/tmp"))
        daemon.process = process
        rpc = mock.Mock()
        daemon.rpc = rpc
        daemon.port = 555
        with mock.patch.object(aria2_runtime.os, "killpg") as killpg, \
                mock.patch.object(aria2_runtime, "_terminate_aria2_port_processes") as terminate_descendants:
            daemon.stop()
        rpc.call.assert_called_once_with("aria2.forceShutdown", timeout=1.0)
        self.assertEqual(
            killpg.call_args_list,
            [mock.call(456, aria2_runtime.signal.SIGTERM), mock.call(456, aria2_runtime.signal.SIGKILL)],
        )
        terminate_descendants.assert_called_once_with(555)

    def test_stop_uses_rpc_and_cleans_descendants_when_wrapper_already_exited(self) -> None:
        process = mock.Mock()
        process.poll.return_value = 0
        rpc = mock.Mock()
        daemon = aria2_runtime.Aria2Daemon("/fake/aria2c", Path("/tmp"))
        daemon.process = process
        daemon.rpc = rpc
        daemon.port = 777
        with mock.patch.object(aria2_runtime, "_terminate_aria2_port_processes") as terminate_descendants:
            daemon.stop()
        rpc.call.assert_called_once_with("aria2.forceShutdown", timeout=1.0)
        terminate_descendants.assert_called_once_with(777)


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


class AlreadyRegisteredRecoveryTests(unittest.TestCase):
    """Regression tests for a real live bug: a torrent going from downloading
    straight to a permanent "error" state that never recovers. Root cause,
    found on a real device: aria2.addTorrent/addUri can legitimately take
    longer than the RPC timeout to parse a large torrent's metadata; a
    client-side timeout there does not mean the add failed server-side --
    aria2 can still finish registering it, leaving a real paused GID we never
    learn about. Every retry then repeats the same add, which aria2 correctly
    rejects as "InfoHash ... is already registered" -- forever, since nothing
    ever adopts or cleans up the orphaned GID. Confirmed live: one stuck
    torrent had accumulated 6 duplicate paused GIDs for the same infohash."""

    def _manager(self, root: Path, rpc: FakeRpc) -> TorrentManager:
        manager = TorrentManager(_build_settings(root), start_worker=False)
        manager._daemon = FakeDaemon(rpc)
        return manager

    def test_add_timeout_uses_a_longer_timeout_than_status_polls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "game")
            manager._tick()
            add_timeouts = [t for m, t in rpc.timeouts if m == "aria2.addTorrent"]
            self.assertEqual(add_timeouts, [torrent_manager.ARIA2_ADD_TIMEOUT_SECONDS])
            self.assertGreater(torrent_manager.ARIA2_ADD_TIMEOUT_SECONDS, 5.0)

    def test_already_registered_adopts_the_existing_gid_instead_of_erroring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "game")

            info_hash = "5a892b21006803f464c35df6d223938c9c85d3e1"
            rpc.add_error = f"InfoHash {info_hash} is already registered."
            rpc.waiting = [{"gid": "orphaned-gid", "infoHash": info_hash, "completedLength": "0"}]

            manager._tick()
            entry = next(iter(manager._torrents.values()))
            self.assertEqual(entry["gid"], "orphaned-gid")
            # Recovered with a real gid in the same tick the scheduler runs,
            # so it's immediately picked up as downloading rather than
            # sitting error'd or waiting for another retry cycle.
            self.assertEqual(entry["status"], "downloading")

    def test_adopts_the_copy_with_the_most_progress_when_several_are_registered(self) -> None:
        # Live evidence showed several duplicate GIDs for the same infohash
        # (repeated timeout/retry cycles) -- adopting the furthest-along one
        # avoids throwing away real progress.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "game")

            info_hash = "9c0fc1ca1fcc2bca0064c7b9645018a49b1feddc"
            rpc.add_error = f"InfoHash {info_hash} is already registered."
            rpc.waiting = [
                {"gid": "stale-empty", "infoHash": info_hash, "completedLength": "0"},
                {"gid": "furthest-along", "infoHash": info_hash, "completedLength": "7245194739"},
                {"gid": "some-progress", "infoHash": info_hash, "completedLength": "100"},
            ]

            manager._tick()
            entry = next(iter(manager._torrents.values()))
            self.assertEqual(entry["gid"], "furthest-along")

    def test_already_registered_with_no_matching_gid_falls_back_to_normal_retry(self) -> None:
        # aria2 says it's registered, but a lookup can't find it (e.g. it
        # finished/errored out and moved to tellStopped in between) -- must
        # still fall back to the existing retry/backoff behavior rather than
        # silently doing nothing.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "game")

            rpc.add_error = "InfoHash 0000000000000000000000000000000000000000 is already registered."
            rpc.waiting = []
            rpc.active = []

            manager._tick()
            entry = next(iter(manager._torrents.values()))
            self.assertEqual(entry["status"], "error")
            self.assertIn("already registered", entry["message"])
            self.assertIsNone(entry["gid"])

    def test_unrelated_add_errors_are_not_treated_as_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "game")

            rpc.add_error = "some unrelated aria2 failure"
            rpc.waiting = [{"gid": "should-not-be-used", "infoHash": "irrelevant", "completedLength": "0"}]

            manager._tick()
            entry = next(iter(manager._torrents.values()))
            self.assertEqual(entry["status"], "error")
            self.assertIsNone(entry["gid"])

    def test_recovery_survives_a_full_retry_cycle_without_erroring_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "game")

            info_hash = "5a892b21006803f464c35df6d223938c9c85d3e1"
            rpc.add_error = f"InfoHash {info_hash} is already registered."
            rpc.waiting = [{"gid": "recovered-gid", "infoHash": info_hash, "completedLength": "12345"}]
            manager._tick()

            entry_id = next(iter(manager._torrents.keys()))
            self.assertEqual(manager._torrents[entry_id]["gid"], "recovered-gid")

            # Now let the recovered GID report real progress on the next poll,
            # like a genuinely resumed download.
            rpc.statuses["recovered-gid"] = {
                "gid": "recovered-gid", "status": "active", "totalLength": "1000",
                "completedLength": "500", "downloadSpeed": "10",
            }
            manager._tick()
            refreshed = manager.snapshot()["torrents"][0]
            self.assertEqual(refreshed["status"], "downloading")
            self.assertEqual(refreshed["progress_percent"], 50.0)


class AsyncAlreadyRegisteredRecoveryTests(unittest.TestCase):
    """Regression tests for a second, distinct trigger of the same
    "already registered" failure family as AlreadyRegisteredRecoveryTests
    above -- found live via a user report of torrents going into an error
    state despite "seemingly downloading just fine right before". Root
    cause: aria2 can *accept* a duplicate addUri/addTorrent for an infohash
    it already has active or paused, handing back a brand-new GID that only
    then fails on its own -- asynchronously, discoverable solely on a later
    aria2.tellStatus poll, never as an exception from the add call itself.
    _recover_from_already_registered previously only ran from the
    synchronous add-time except block (in _add_torrent_via_rpc /
    _add_magnet_via_rpc), so this async failure fell through to a plain
    retry that discarded the doomed GID and added yet another one -- which
    failed the exact same asynchronous way, forever, even though the real
    download (under its original, healthy GID elsewhere in aria2) was fine
    the entire time. Reproduced deterministically against a real aria2c
    binary before being fixed in _query_torrent_via_rpc /
    _apply_aria2_status_locked."""

    def _manager(self, root: Path, rpc: FakeRpc) -> TorrentManager:
        manager = TorrentManager(_build_settings(root), start_worker=False)
        manager._daemon = FakeDaemon(rpc)
        return manager

    def _inject_queued_entry_with_gid(self, manager: TorrentManager, entry_id: str, gid: str) -> None:
        with manager._lock:
            manager._torrents[entry_id] = {
                "id": entry_id,
                "name": "Queued Entry",
                "torrent_file": "",
                "magnet_uri": "magnet:?xt=urn:btih:deadbeefdeadbeefdeadbeefdeadbeefdeadbeef&dn=x",
                "download_dir": str(manager.settings.userdata_root / "downloads"),
                "status": "queued",
                "message": "",
                "added_at": "2026-01-01T00:00:00+00:00",
                "completed_at": None,
                "total_bytes": 0,
                "completed_bytes": 0,
                "progress_percent": 0.0,
                "files": [],
                "queue_position": 1,
                "retry_count": 0,
                "retry_at": 0.0,
                "last_error": "",
                "gid": gid,
                "force_started": False,
                "seeding": False,
                "download_speed_bps": 0,
                "upload_speed_bps": 0,
                "num_seeders": 0,
                "connections": 0,
                "eta_seconds": None,
            }

    def test_async_already_registered_on_status_poll_adopts_the_healthy_gid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "game")

            manager._tick()  # normal add; scheduler unpauses it in the same tick
            entry_id = next(iter(manager._torrents.keys()))
            doomed_gid = manager._torrents[entry_id]["gid"]
            self.assertEqual(manager._torrents[entry_id]["status"], "downloading")

            # The doomed GID now asynchronously errors out on its own -- no
            # add-time exception, just a later tellStatus response.
            info_hash = "5a892b21006803f464c35df6d223938c9c85d3e1"
            rpc.statuses[doomed_gid] = {
                "gid": doomed_gid,
                "status": "error",
                "errorMessage": f"InfoHash {info_hash} is already registered.",
                "totalLength": "0",
                "completedLength": "0",
                "downloadSpeed": "0",
            }
            rpc.waiting = [{"gid": "healthy-real-gid", "infoHash": info_hash, "completedLength": "999999"}]

            manager._tick()

            entry = manager._torrents[entry_id]
            self.assertEqual(entry["gid"], "healthy-real-gid")
            # Stays downloading throughout -- this is the actual live bug:
            # a torrent that never stops "seemingly downloading fine right
            # before erroring" instead of flapping to "error" and back.
            self.assertEqual(entry["status"], "downloading")
            self.assertEqual(entry["retry_count"], 0)
            self.assertEqual(entry["last_error"], "")
            # The doomed GID must be cleaned up out of aria2, not left to
            # accumulate as dead history.
            self.assertIn([doomed_gid], rpc.method_calls("aria2.forceRemove"))

    def test_async_already_registered_with_no_matching_gid_falls_back_to_a_plain_retry(self) -> None:
        # If the lookup can't find any live registration for the infohash
        # (e.g. it finished/errored out in between), this must still behave
        # like an ordinary error -- not silently do nothing forever.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "game")

            manager._tick()
            entry_id = next(iter(manager._torrents.keys()))
            doomed_gid = manager._torrents[entry_id]["gid"]

            rpc.statuses[doomed_gid] = {
                "gid": doomed_gid,
                "status": "error",
                "errorMessage": "InfoHash 0000000000000000000000000000000000000000 is already registered.",
                "totalLength": "0",
                "completedLength": "0",
                "downloadSpeed": "0",
            }
            rpc.waiting = []
            rpc.active = []

            manager._tick()

            entry = manager._torrents[entry_id]
            self.assertEqual(entry["status"], "error")
            self.assertIn("already registered", entry["message"])
            self.assertIsNone(entry["gid"])
            self.assertEqual(entry["retry_count"], 1)

    def test_unrelated_status_errors_are_not_treated_as_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "game")

            manager._tick()
            entry_id = next(iter(manager._torrents.keys()))
            doomed_gid = manager._torrents[entry_id]["gid"]

            rpc.statuses[doomed_gid] = {
                "gid": doomed_gid,
                "status": "error",
                "errorMessage": "some unrelated aria2 failure",
                "totalLength": "0",
                "completedLength": "0",
                "downloadSpeed": "0",
            }
            rpc.waiting = [{"gid": "should-not-be-used", "infoHash": "irrelevant", "completedLength": "0"}]

            manager._tick()

            entry = manager._torrents[entry_id]
            self.assertEqual(entry["status"], "error")
            self.assertIsNone(entry["gid"])

    def test_unpause_failure_is_silently_ignored_when_the_gid_is_already_active(self) -> None:
        # _pick_startable_gids_locked can hand Phase D a "queued" entry whose
        # gid a same-tick recovery just retargeted onto an aria2 download
        # that turns out to already be active (not paused) -- confirmed
        # live: aria2.unpause on such a gid returns a harmless "cannot be
        # unpaused now" error that would otherwise spam stderr every tick.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            rpc.unpause_error = "GID#somegid cannot be unpaused now"
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            self._inject_queued_entry_with_gid(manager, "already-active", "somegid")
            rpc.statuses["somegid"] = {
                "gid": "somegid", "status": "paused", "totalLength": "0",
                "completedLength": "0", "downloadSpeed": "0",
            }

            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                manager._tick()

            self.assertNotIn("cannot be unpaused now", captured.getvalue())
            self.assertEqual(manager._torrents["already-active"]["status"], "downloading")

    def test_unrelated_unpause_failure_is_still_logged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            rpc.unpause_error = "some unrelated aria2 failure"
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            self._inject_queued_entry_with_gid(manager, "queued-entry", "somegid")
            rpc.statuses["somegid"] = {
                "gid": "somegid", "status": "paused", "totalLength": "0",
                "completedLength": "0", "downloadSpeed": "0",
            }

            captured = io.StringIO()
            with contextlib.redirect_stderr(captured):
                manager._tick()

            self.assertIn("some unrelated aria2 failure", captured.getvalue())


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
            # "b"'s completed_at needs a second tick reporting the same gid still
            # finished before it's trusted (see TorrentCompletedNotificationTests) --
            # everything else here is settled after just the one tick above.
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
            # The watched folder is no longer user-configurable -- a restart
            # self-heals it back to the install-root default even if an
            # older persisted config had a custom value.
            self.assertEqual(restarted.snapshot()["settings"]["directory"], str(default_torrent_directory(_build_settings(root))))
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


class TorrentAria2ReconciliationTests(unittest.TestCase):
    """The UI must be an honest mirror of what aria2 is actually doing, not
    just what this manager remembers adding -- confirmed live: a removal RPC
    that couldn't land while aria2 was busy on a slow write left a real,
    still-writing download orphaned in aria2 with zero representation
    anywhere in the UI once its entry had already been dropped from our own
    tracking. Covers the pending-removal retry (delete/cancel/clear) and
    orphan-gid adoption (aria2.tellActive/tellWaiting)."""

    def _manager(self, root: Path, rpc: FakeRpc) -> TorrentManager:
        manager = TorrentManager(_build_settings(root), start_worker=False)
        manager._daemon = FakeDaemon(rpc)
        return manager

    def test_delete_queues_gid_for_retry_when_aria2_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "a")
            manager._tick()
            entry = manager.snapshot()["torrents"][0]
            with manager._lock:
                gid = manager._torrents[entry["id"]]["gid"]

            rpc.remove_error = "timed out"
            result = manager.delete(entry["id"])
            # Deletion from the UI is immediate/unconditional -- the entry is
            # gone from the snapshot right away regardless of whether aria2
            # could be reached.
            self.assertEqual(result["status"], "deleted")
            self.assertEqual(manager.snapshot()["torrents"], [])
            with manager._lock:
                self.assertIn(gid, manager._pending_removal_gids)

            # aria2 recovers; the next tick must retry and actually drain it,
            # not leave the download running forever unmanaged.
            rpc.remove_error = None
            manager._tick()
            with manager._lock:
                self.assertNotIn(gid, manager._pending_removal_gids)
            self.assertIn(("aria2.forceRemove", [gid]), rpc.calls)

    def test_delete_in_progress_torrent_is_not_re_added_by_a_concurrent_rescan(self) -> None:
        # Issue #42: deleting an in-progress torrent "deletes it and adds it
        # right back". The watched .torrent file was unlinked only after the
        # lock was released, so the 3s poll thread's watch-folder rescan
        # slipped into that gap and re-registered it as a fresh "queued" row.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "shape-of-water")
            manager._tick()
            entry = manager.snapshot()["torrents"][0]

            original_remove = manager._remove_from_aria2

            def racing_remove(gid, *args, **kwargs):
                # Stand in for the poll thread grabbing the lock during the
                # window between the entry being dropped and aria2 confirming
                # the removal.
                with manager._lock:
                    manager._scan_watch_directory_locked(dict(manager._config))
                return original_remove(gid, *args, **kwargs)

            manager._remove_from_aria2 = racing_remove

            result = manager.delete(entry["id"])
            self.assertEqual(result["status"], "deleted")
            self.assertEqual(manager.snapshot()["torrents"], [])
            self.assertFalse((watch / "shape-of-water.torrent").exists())

    def test_delete_does_not_leave_an_adopted_download_when_removal_races_a_tick(self) -> None:
        # Issue #42: the "weird Adopted download" torrent. While delete()'s
        # forceRemove RPC was in flight, a concurrent tick's
        # _adopt_orphaned_gids saw the still-registered gid with no entry and
        # resurrected it as a source-less "Adopted download" row.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "shape-of-water")
            manager._tick()
            entry = manager.snapshot()["torrents"][0]
            with manager._lock:
                gid = manager._torrents[entry["id"]]["gid"]
            rpc.statuses[gid].update({"status": "active", "totalLength": "1000", "completedLength": "500"})
            rpc.active = [rpc.statuses[gid]]

            original_remove = manager._remove_from_aria2

            def racing_remove(target_gid, *args, **kwargs):
                manager._adopt_orphaned_gids(rpc)
                return original_remove(target_gid, *args, **kwargs)

            manager._remove_from_aria2 = racing_remove

            manager.delete(entry["id"])
            self.assertEqual(manager.snapshot()["torrents"], [])
            rpc.active = []
            manager._tick()
            self.assertEqual(manager.snapshot()["torrents"], [])

    def test_gid_not_found_on_removal_counts_as_already_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "a")
            manager._tick()
            entry = manager.snapshot()["torrents"][0]
            with manager._lock:
                gid = manager._torrents[entry["id"]]["gid"]
            # Simulate aria2 having already forgotten this gid entirely (e.g.
            # it finished being torn down by something else) rather than
            # being merely busy -- forceRemove/removeDownloadResult raise
            # "not found" for any gid FakeRpc hasn't registered.
            del rpc.statuses[gid]

            result = manager.delete(entry["id"])
            self.assertEqual(result["status"], "deleted")
            with manager._lock:
                self.assertNotIn(gid, manager._pending_removal_gids)

    def test_cancel_and_clear_also_queue_pending_removal_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "a")
            _write_torrent(watch, "b")
            manager.update_settings({"max_concurrent_downloads": 2})
            manager._tick()
            entries = {e["name"]: e for e in manager.snapshot()["torrents"]}
            with manager._lock:
                gid_a = manager._torrents[entries["a"]["id"]]["gid"]
                gid_b = manager._torrents[entries["b"]["id"]]["gid"]

            rpc.remove_error = "timed out"
            manager.cancel(entries["a"]["id"])
            with manager._lock:
                self.assertIn(gid_a, manager._pending_removal_gids)

            manager.clear({"scope": "all", "delete_from_ui": True})
            with manager._lock:
                self.assertIn(gid_b, manager._pending_removal_gids)
            self.assertEqual(manager.snapshot()["torrents"], [])

    def test_adopts_orphaned_gid_into_a_manageable_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            # aria2 already knows about this download -- e.g. left running
            # after an earlier removal RPC failed -- but this manager has
            # never added it and has no entry for it at all.
            rpc.statuses["orphan-gid"] = {
                "gid": "orphan-gid",
                "status": "active",
                "totalLength": "1000",
                "completedLength": "400",
                "downloadSpeed": "20",
                "dir": "/media/roms_modern/torrents",
                "bittorrent": {"info": {"name": "Orphaned Movie"}},
            }
            rpc.active = [rpc.statuses["orphan-gid"]]

            manager._tick()

            entries = manager.snapshot()["torrents"]
            self.assertEqual(len(entries), 1)
            adopted = entries[0]
            self.assertEqual(adopted["name"], "Orphaned Movie")
            self.assertEqual(adopted["status"], "downloading")
            self.assertEqual(adopted["progress_percent"], 40.0)
            self.assertEqual(adopted["download_dir"], "/media/roms_modern/torrents")

            # Once adopted, it's a perfectly normal entry -- controllable
            # like anything else the manager itself added.
            result = manager.delete(adopted["id"])
            self.assertEqual(result["status"], "deleted")
            self.assertEqual(manager.snapshot()["torrents"], [])
            self.assertIn(("aria2.forceRemove", ["orphan-gid"]), rpc.calls)

    def test_does_not_readopt_a_gid_it_already_knows_about(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "a")
            manager._tick()
            with manager._lock:
                gid = next(iter(manager._torrents.values()))["gid"]
            # This tick's own tracked download also happens to show up in
            # tellActive, exactly like it would against a real aria2c.
            rpc.active = [rpc.statuses[gid]]

            manager._tick()

            self.assertEqual(len(manager.snapshot()["torrents"]), 1)

    def test_does_not_adopt_magnet_metadata_gid_pending_followed_by_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            rpc.statuses["metadata-gid"] = {
                "gid": "metadata-gid",
                "status": "active",
                "totalLength": "500",
                "completedLength": "500",
                "downloadSpeed": "0",
                "followedBy": ["content-gid"],
            }
            rpc.active = [rpc.statuses["metadata-gid"]]

            manager._tick()

            self.assertEqual(manager.snapshot()["torrents"], [])

    def test_does_not_adopt_content_gid_while_tracked_metadata_parent_is_handing_off(self) -> None:
        """A magnet's content GID can appear in tellActive one poll before
        tellStatus exposes it in the tracked metadata GID's followedBy list.
        The reverse `following` link must keep reconciliation from adopting a
        second registry row during that gap.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            added = manager.add_magnet(_MAGNET_URI)
            manager._tick()
            with manager._lock:
                metadata_gid = manager._torrents[added["id"]]["gid"]

            rpc.statuses["content-gid"] = {
                "gid": "content-gid",
                "status": "active",
                "totalLength": "9000000000",
                "completedLength": "468000000",
                "downloadSpeed": "2700000",
                "numSeeders": "8",
                "connections": "24",
                "following": metadata_gid,
                "bittorrent": {"info": {"name": "Single download"}},
            }
            rpc.active = [rpc.statuses["content-gid"]]

            # The child is visible, but the parent's tellStatus response has
            # not caught up with a followedBy value yet.
            manager._tick()
            self.assertEqual(len(manager.snapshot()["torrents"]), 1)
            with manager._lock:
                self.assertEqual(manager._torrents[added["id"]]["gid"], metadata_gid)

            # Once followedBy arrives, the existing row moves to the content
            # GID instead of a second row having already adopted it.
            rpc.statuses[metadata_gid]["status"] = "complete"
            rpc.statuses[metadata_gid]["followedBy"] = ["content-gid"]
            manager._tick()
            self.assertEqual(len(manager.snapshot()["torrents"]), 1)
            with manager._lock:
                self.assertEqual(manager._torrents[added["id"]]["gid"], "content-gid")

    def test_collapses_existing_adopted_row_that_shares_a_source_rows_gid(self) -> None:
        """Self-heal duplicate rows produced by the old handoff race without
        removing or restarting their one shared aria2 download.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            added = manager.add_magnet(_MAGNET_URI)
            manager._tick()
            with manager._lock:
                original = manager._torrents[added["id"]]
                shared_gid = original["gid"]
                adopted = dict(original)
                adopted.update(
                    {
                        "id": "adopted-copy",
                        "torrent_file": "",
                        "magnet_uri": "",
                        "added_at": "2099-01-01T00:00:00+00:00",
                        "queue_position": original["queue_position"] + 1,
                    }
                )
                manager._torrents[adopted["id"]] = adopted

            manager._tick()

            rows = manager.snapshot()["torrents"]
            self.assertEqual([row["id"] for row in rows], [added["id"]])
            self.assertNotIn([shared_gid], rpc.method_calls("aria2.forceRemove"))

    def test_does_not_adopt_orphaned_gid_sharing_a_known_info_hash_even_when_following_is_unknown(self) -> None:
        """Belt-and-suspenders on top of the `following`-chain guard: even if
        the metadata GID has already dropped out of tellActive/tellWaiting
        (so `following` can't be matched against `known_gids` at all), a
        matching BitTorrent info-hash alone must still block adoption --
        confirmed live on a real drone: this exact gap let 4 of 6 magnet
        torrents each grow a source-less duplicate row that eventually
        calcified into a permanent extra `error` entry.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            added = manager.add_magnet(_MAGNET_URI)
            manager._tick()
            with manager._lock:
                info_hash = manager._torrents[added["id"]]["info_hash"]
            self.assertTrue(info_hash)

            rpc.statuses["content-gid"] = {
                "gid": "content-gid",
                "status": "active",
                "totalLength": "9000000000",
                "completedLength": "468000000",
                "downloadSpeed": "2700000",
                # Points at a metadata gid this manager never tracked/has
                # already forgotten -- the `following in known_gids` guard
                # alone cannot catch this.
                "following": "long-gone-metadata-gid",
                "infoHash": info_hash,
                "bittorrent": {"info": {"name": "Some Test File"}},
            }
            rpc.active = [rpc.statuses["content-gid"]]

            manager._tick()

            self.assertEqual(len(manager.snapshot()["torrents"]), 1)

    def test_collapses_orphan_row_sharing_info_hash_after_its_gid_has_gone_stale(self) -> None:
        """The gid-keyed dedup pass alone can permanently miss a duplicate
        whose gid has since diverged from the real entry's -- e.g. it already
        errored, which resets `gid` back to `None` (`_schedule_retry_locked`)
        -- confirmed live: 3 of 4 duplicate torrents on a real drone were
        stuck in exactly this shape, forever, since a source-less row has
        nothing to ever re-add itself from. info_hash survives that.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            added = manager.add_magnet(_MAGNET_URI)
            manager._tick()
            with manager._lock:
                original = manager._torrents[added["id"]]
                info_hash = original["info_hash"]
                self.assertTrue(info_hash)
                orphan = dict(original)
                orphan.update(
                    {
                        "id": "stale-orphan-copy",
                        "torrent_file": "",
                        "magnet_uri": "",
                        "gid": None,
                        "status": "error",
                        "retry_at": 0.0,
                        "added_at": "2099-01-01T00:00:00+00:00",
                        "queue_position": original["queue_position"] + 1,
                    }
                )
                manager._torrents[orphan["id"]] = orphan

            manager._tick()

            rows = manager.snapshot()["torrents"]
            self.assertEqual([row["id"] for row in rows], [added["id"]])


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

    def test_does_not_fire_for_a_one_tick_finished_blip_that_then_reverts(self) -> None:
        # Regression test for a real live bug: two magnet-added torrents got
        # completed_at set (and a "download completed" notification fired)
        # while only 11-24% through their actual content. Evidence pointed at
        # aria2 briefly reporting the same gid as "finished" against a tiny
        # (metadata-sized) total for a single poll, before its own totals
        # settled onto the real, much larger content -- not necessarily via a
        # missed `followedBy` (that handoff is covered separately below), but
        # any transient single-tick "finished" reading for a gid that then
        # reverts to genuinely incomplete must never be trusted.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            manager.add_magnet(_MAGNET_URI)
            manager._tick()
            with manager._lock:
                gid = next(iter(manager._torrents.values()))["gid"]

            with mock.patch.object(torrent_manager, "_notifications") as fake_notifications:
                # Tick: gid reports "finished" against a tiny (metadata-sized) total.
                rpc.statuses[gid] = {
                    "gid": gid, "status": "active", "totalLength": "1192915", "completedLength": "1192915",
                    "downloadSpeed": "0",
                }
                manager._tick()
                fake_notifications.record_event.assert_not_called()
                self.assertIsNone(manager.snapshot()["torrents"][0]["completed_at"])

                # Same gid, next tick: the real (much larger, mostly incomplete)
                # totals show up instead of a second "finished" confirmation.
                rpc.statuses[gid] = {
                    "gid": gid, "status": "active", "totalLength": "477183500385", "completedLength": "34865152",
                    "downloadSpeed": "1048576",
                }
                manager._tick()
                fake_notifications.record_event.assert_not_called()
                self.assertIsNone(manager.snapshot()["torrents"][0]["completed_at"])
                self.assertEqual(manager.snapshot()["torrents"][0]["status"], "downloading")

    def test_fires_once_the_same_gid_confirms_finished_on_two_consecutive_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            manager.add_magnet(_MAGNET_URI)
            manager._tick()
            with manager._lock:
                gid = next(iter(manager._torrents.values()))["gid"]
            rpc.statuses[gid] = {
                "gid": gid, "status": "active", "totalLength": "477183500385", "completedLength": "477183500385",
                "downloadSpeed": "0",
            }
            with mock.patch.object(torrent_manager, "_notifications") as fake_notifications:
                manager._tick()  # 1st "finished" observation -- not yet confirmed
                fake_notifications.record_event.assert_not_called()
                manager._tick()  # same gid, still finished -- confirmed
                fake_notifications.record_event.assert_called_once()
                manager._tick()  # already notified -- no re-fire
                fake_notifications.record_event.assert_called_once()
            self.assertIsNotNone(manager.snapshot()["torrents"][0]["completed_at"])

    def test_does_not_double_notify_two_entries_sharing_the_same_info_hash(self) -> None:
        """Defense in depth for when a duplicate registry row (see
        TorrentAria2ReconciliationTests) slips past both dedup passes and
        independently reaches "finished" under its own gid -- confirmed
        live: two registry rows that both ended up tracking one real aria2
        download each independently sent their own "Torrent download
        completed" email for the same torrent. Each entry still gets its
        own completed_at (so its row stops looking stuck), only the second
        notification is suppressed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = self._manager(root, rpc)
            shared_info_hash = "deadbeef" * 5
            with mock.patch.object(torrent_manager, "_notifications") as fake_notifications:
                with manager._lock:
                    manager._torrents["entry-a"] = {
                        "id": "entry-a",
                        "name": "Duplicate Torrent",
                        "gid": "gid-a",
                        "info_hash": shared_info_hash,
                        "_pending_complete_gid": "gid-a",
                        "completed_at": None,
                    }
                    manager._confirm_finished_and_notify_locked(manager._torrents["entry-a"])
                    manager._torrents["entry-b"] = {
                        "id": "entry-b",
                        "name": "Duplicate Torrent",
                        "gid": "gid-b",
                        "info_hash": shared_info_hash,
                        "_pending_complete_gid": "gid-b",
                        "completed_at": None,
                    }
                    manager._confirm_finished_and_notify_locked(manager._torrents["entry-b"])
                fake_notifications.record_event.assert_called_once()
            self.assertIsNotNone(manager._torrents["entry-a"]["completed_at"])
            self.assertIsNotNone(manager._torrents["entry-b"]["completed_at"])


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

    def test_add_magnet_is_idempotent_by_info_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(Path(tmp), FakeRpc())
            first = manager.add_magnet(_MAGNET_URI)
            same_torrent_different_name = (
                "magnet:?dn=Renamed+Copy&xt=urn:btih:C12FE1C06BBA254A9DC9F519B335AA7C1367A88A"
            )

            duplicate = manager.add_magnet(same_torrent_different_name)

            self.assertEqual(duplicate["status"], "already_exists")
            self.assertEqual(duplicate["id"], first["id"])
            self.assertEqual(len(manager.snapshot()["torrents"]), 1)

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


def _completed_torrent_manager(root: Path, rpc: FakeRpc, files_by_name: dict):
    """Build a manager with one .torrent per name in ``files_by_name``,
    tick it to completion, and stamp aria2's reported ``files`` for each.
    Shared by TorrentFileManagementTests and TorrentMoveJobAsyncTests."""
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


def _drain_move_jobs(manager: TorrentManager, max_ticks: int = 200) -> None:
    """Run the move worker's own tick to completion synchronously, the
    test-side equivalent of _move_worker's background loop -- move_files()
    itself only enqueues now (see torrent_manager.py), the actual
    shutil.move work happens in _move_tick()."""

    for _ in range(max_ticks):
        with manager._lock:
            if manager._next_move_job_entry_locked() is None:
                return
        manager._move_tick()
    raise AssertionError("move job(s) did not drain within max_ticks -- possible infinite loop")


class TorrentFileManagementTests(unittest.TestCase):
    _completed_manager = staticmethod(_completed_torrent_manager)
    _drain_move_jobs = staticmethod(_drain_move_jobs)

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
            self.assertEqual(result["status"], "queued")
            self._drain_move_jobs(manager)
            self.assertTrue((dest / "a.bin").exists())
            self.assertFalse(payload_a.exists())
            # The shared download dir (and the sibling torrent's own file) is
            # never wholesale-deleted -- only the specifically known file(s).
            self.assertTrue(payload_b.exists())
            # cleanup=True + full success also removes the torrent entirely
            # (matching the original synchronous behavior).
            self.assertNotIn(entry_a["id"], manager._torrents)
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
            self.assertEqual(result["status"], "queued")
            self._drain_move_jobs(manager)
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
            self.assertEqual(result["status"], "queued")
            self._drain_move_jobs(manager)
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
            self.assertEqual(result["status"], "queued")
            self._drain_move_jobs(manager)
            with manager._lock:
                move_job = manager._torrents[entry["id"]]["move_job"]
            # One file succeeded, so the job as a whole still finishes
            # (not "failed") -- but cleanup never fires on anything less
            # than a fully-clean batch, and the torrent stays in the list.
            self.assertEqual(move_job["status"], "complete")
            self.assertEqual(len(move_job["errors"]), 1)
            self.assertIn(entry["id"], manager._torrents)

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
            self.assertEqual(result["status"], "queued")
            self._drain_move_jobs(manager)
            with manager._lock:
                move_job = manager._torrents[entry["id"]]["move_job"]
            self.assertTrue((dest / "a.bin").exists())
            self.assertEqual(len(move_job["errors"]), 1)
            self.assertEqual(len(move_job["moved_files"]), 1)  # a.bin succeeded; cleanup itself never ran
            self.assertIn(entry["id"], manager._torrents)

    def test_move_files_all_selected_missing_marks_job_failed(self) -> None:
        # Distinct from the mixed-success "partial" case above: zero progress
        # at all is a real failure, not a quiet "finished with errors". Use a
        # real file so the torrent legitimately reaches "complete" (non-zero
        # total), then remove it before the move to simulate "known but
        # vanished by move time" without hitting a zero-total edge case.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            payload = watch / "a.bin"
            payload.write_bytes(b"data")
            manager, _ = self._completed_manager(root, FakeRpc(), {"a": [payload]})
            entry = manager.snapshot()["torrents"][0]
            self.assertEqual(entry["status"], "complete")
            payload.unlink()
            dest = root / "moved"
            manager.move_files(entry["id"], [str(payload)], str(dest), cleanup=False)
            self._drain_move_jobs(manager)
            with manager._lock:
                move_job = manager._torrents[entry["id"]]["move_job"]
            self.assertEqual(move_job["status"], "failed")
            self.assertEqual(len(move_job["errors"]), 1)

    def test_move_files_default_flattens_nested_layout(self) -> None:
        # preserve_structure defaults to False -- unchanged historical
        # behavior, callers that don't pass the new kwarg must keep flattening.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            sub = watch / "Pack" / "Sub"
            sub.mkdir(parents=True)
            file1 = sub / "one.bin"
            file1.write_bytes(b"one")
            manager, _ = self._completed_manager(root, FakeRpc(), {"Pack": [file1]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"
            manager.move_files(entry["id"], [str(file1)], str(dest), cleanup=False)
            self._drain_move_jobs(manager)
            self.assertTrue((dest / "one.bin").exists())
            self.assertFalse((dest / "Pack").exists())

    def test_move_files_preserve_structure_recreates_nested_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            season1 = watch / "Show Pack" / "Season 1"
            season2 = watch / "Show Pack" / "Season 2"
            season1.mkdir(parents=True)
            season2.mkdir(parents=True)
            file1 = season1 / "e01.mkv"
            file2 = season2 / "e01.mkv"
            file1.write_bytes(b"one")
            file2.write_bytes(b"two")
            manager, _ = self._completed_manager(root, FakeRpc(), {"Show Pack": [file1, file2]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"
            manager.move_files(
                entry["id"], [str(file1), str(file2)], str(dest), cleanup=False, preserve_structure=True
            )
            self._drain_move_jobs(manager)
            self.assertTrue((dest / "Show Pack" / "Season 1" / "e01.mkv").exists())
            self.assertTrue((dest / "Show Pack" / "Season 2" / "e01.mkv").exists())
            self.assertFalse(file1.exists())
            self.assertFalse(file2.exists())

    def test_move_files_preserve_structure_only_creates_folders_for_selected_files(self) -> None:
        # A partial selection (some files in a folder unchecked) must not drag
        # in folders the user didn't select anything from.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            season1 = watch / "Show Pack" / "Season 1"
            season2 = watch / "Show Pack" / "Season 2"
            season1.mkdir(parents=True)
            season2.mkdir(parents=True)
            file1 = season1 / "e01.mkv"
            file2 = season2 / "e01.mkv"
            file1.write_bytes(b"one")
            file2.write_bytes(b"two")
            manager, _ = self._completed_manager(root, FakeRpc(), {"Show Pack": [file1, file2]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"
            manager.move_files(
                entry["id"], [str(file1)], str(dest), cleanup=False, preserve_structure=True
            )
            self._drain_move_jobs(manager)
            self.assertTrue((dest / "Show Pack" / "Season 1" / "e01.mkv").exists())
            self.assertFalse((dest / "Show Pack" / "Season 2").exists())

    def test_move_files_preserve_structure_collision_suffixes_within_same_subfolder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            subfolder = watch / "Pack" / "Sub"
            subfolder.mkdir(parents=True)
            file1 = subfolder / "one.bin"
            file1.write_bytes(b"new")
            manager, _ = self._completed_manager(root, FakeRpc(), {"Pack": [file1]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"
            existing = dest / "Pack" / "Sub" / "one.bin"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"already there")
            manager.move_files(
                entry["id"], [str(file1)], str(dest), cleanup=False, preserve_structure=True
            )
            self._drain_move_jobs(manager)
            # A collision at the destination renames the incoming file --
            # it must stay in the same recreated subfolder, not fall back to
            # the destination root.
            self.assertTrue(existing.exists())
            self.assertEqual(existing.read_bytes(), b"already there")
            suffixed = dest / "Pack" / "Sub" / "one (2).bin"
            self.assertTrue(suffixed.exists())
            self.assertEqual(suffixed.read_bytes(), b"new")

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
            self._drain_move_jobs(manager)
            manager.move_files(entries["b"]["id"], [str(payload_b)], str(dest2), cleanup=False)
            self._drain_move_jobs(manager)
            recent = manager.snapshot()["recent_move_locations"]
            self.assertEqual(recent[:2], [str(dest2.resolve()), str(dest1.resolve())])


class TorrentMoveJobAsyncTests(unittest.TestCase):
    """The async/resumable/notified redesign of "Move Downloaded Files" --
    enqueue-only move_files(), the single global _move_tick() mover, restart
    resume via _restore_state(), and the 4 notification types. See
    TorrentFileManagementTests for the file-manipulation-outcome coverage
    (collision suffixing, preserve_structure, cleanup) -- this class covers
    the scheduling/persistence/notification behavior layered on top."""

    def test_enqueue_does_not_move_synchronously(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            payload = watch / "a.bin"
            payload.write_bytes(b"hello")
            manager, _ = _completed_torrent_manager(root, FakeRpc(), {"a": [payload]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"
            result = manager.move_files(entry["id"], [str(payload)], str(dest), cleanup=False)
            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["move_job"]["status"], "queued")
            self.assertEqual(result["move_job"]["total_files"], 1)
            # Only _move_tick() (the background worker) does the actual
            # filesystem work -- move_files() itself must return immediately.
            self.assertTrue(payload.exists())
            self.assertFalse((dest / "a.bin").exists())

    def test_already_in_progress_rejects_a_second_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            payload = watch / "a.bin"
            payload.write_bytes(b"hello")
            manager, _ = _completed_torrent_manager(root, FakeRpc(), {"a": [payload]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"
            first = manager.move_files(entry["id"], [str(payload)], str(dest), cleanup=False)
            self.assertEqual(first["status"], "queued")
            second = manager.move_files(entry["id"], [str(payload)], str(dest), cleanup=False)
            self.assertEqual(second["status"], "already_in_progress")
            # Draining still only ever sees the one real job/file.
            _drain_move_jobs(manager)
            with manager._lock:
                move_job = manager._torrents[entry["id"]]["move_job"]
            self.assertEqual(len(move_job["moved_files"]), 1)

    def test_torrent_move_started_fires_at_enqueue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            payload = watch / "a.bin"
            payload.write_bytes(b"hello")
            manager, _ = _completed_torrent_manager(root, FakeRpc(), {"a": [payload]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"
            with mock.patch.object(torrent_manager, "_notifications") as fake_notifications:
                manager.move_files(entry["id"], [str(payload)], str(dest), cleanup=False)
                fake_notifications.record_event.assert_called_once()
                self.assertEqual(fake_notifications.record_event.call_args[0][1], "torrent_move_started")

    def test_torrent_move_finished_and_failed_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            good = watch / "good.bin"
            good.write_bytes(b"data")
            manager, _ = _completed_torrent_manager(root, FakeRpc(), {"a": [good]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"
            with mock.patch.object(torrent_manager, "_notifications") as fake_notifications:
                manager.move_files(entry["id"], [str(good)], str(dest), cleanup=False)
                _drain_move_jobs(manager)
                fired = [call.args[1] for call in fake_notifications.record_event.call_args_list]
            self.assertEqual(fired, ["torrent_move_started", "torrent_move_finished"])

        with tempfile.TemporaryDirectory() as tmp2:
            root2 = Path(tmp2)
            watch2 = root2 / "watch"
            watch2.mkdir(parents=True)
            payload2 = watch2 / "a.bin"
            payload2.write_bytes(b"data")
            manager2, _ = _completed_torrent_manager(root2, FakeRpc(), {"a": [payload2]})
            entry2 = manager2.snapshot()["torrents"][0]
            payload2.unlink()  # known to aria2, gone by move time -- zero real progress
            dest2 = root2 / "moved"
            with mock.patch.object(torrent_manager, "_notifications") as fake_notifications:
                manager2.move_files(entry2["id"], [str(payload2)], str(dest2), cleanup=False)
                _drain_move_jobs(manager2)
                fired2 = [call.args[1] for call in fake_notifications.record_event.call_args_list]
            self.assertEqual(fired2, ["torrent_move_started", "torrent_move_failed"])

    def test_single_global_mover_processes_one_file_at_a_time_across_torrents(self) -> None:
        # Per explicit instruction: even with multiple torrents' moves
        # queued, only one file moves at a time -- one shared worker loop
        # picking the globally-oldest job's next file, not one thread/slot
        # per torrent.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            a1, a2 = watch / "a1.bin", watch / "a2.bin"
            b1, b2 = watch / "b1.bin", watch / "b2.bin"
            for p in (a1, a2, b1, b2):
                p.write_bytes(b"x")
            manager, _ = _completed_torrent_manager(root, FakeRpc(), {"a": [a1, a2], "b": [b1, b2]})
            entries = {e["name"]: e for e in manager.snapshot()["torrents"]}
            dest = root / "moved"
            result_a = manager.move_files(entries["a"]["id"], [str(a1), str(a2)], str(dest), cleanup=False)
            result_b = manager.move_files(entries["b"]["id"], [str(b1), str(b2)], str(dest), cleanup=False)
            self.assertEqual(result_a["status"], "queued")
            self.assertEqual(result_b["status"], "queued")

            manager._move_tick()
            with manager._lock:
                job_a = manager._torrents[entries["a"]["id"]]["move_job"]
                job_b = manager._torrents[entries["b"]["id"]]["move_job"]
            # "a" was enqueued first, so exactly one of its two files moved;
            # "b" (enqueued later) hasn't been touched at all yet.
            self.assertEqual(len(job_a["moved_files"]), 1)
            self.assertEqual(len(job_b["moved_files"]), 0)

            manager._move_tick()
            with manager._lock:
                job_a = manager._torrents[entries["a"]["id"]]["move_job"]
                job_b = manager._torrents[entries["b"]["id"]]["move_job"]
            # "a" fully drains before "b" gets its first file -- simple
            # FIFO-per-job, not round-robin-per-file.
            self.assertEqual(len(job_a["moved_files"]), 2)
            self.assertEqual(job_a["status"], "complete")
            self.assertEqual(len(job_b["moved_files"]), 0)

            _drain_move_jobs(manager)
            with manager._lock:
                job_b = manager._torrents[entries["b"]["id"]]["move_job"]
            self.assertEqual(len(job_b["moved_files"]), 2)

    def test_destination_unavailable_mid_job_fails_without_losing_remaining_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            file1, file2 = watch / "one.bin", watch / "two.bin"
            file1.write_bytes(b"one")
            file2.write_bytes(b"two")
            manager, _ = _completed_torrent_manager(root, FakeRpc(), {"a": [file1, file2]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"
            with mock.patch.object(torrent_manager, "_notifications") as fake_notifications:
                manager.move_files(entry["id"], [str(file1), str(file2)], str(dest), cleanup=False)
                manager._move_tick()  # moves file1 successfully
                shutil.rmtree(dest)  # simulate the destination mount vanishing
                manager._move_tick()  # attempts file2, finds the destination gone
                fired = [call.args[1] for call in fake_notifications.record_event.call_args_list]
            self.assertEqual(fired, ["torrent_move_started", "torrent_move_failed"])
            with manager._lock:
                move_job = manager._torrents[entry["id"]]["move_job"]
            self.assertEqual(move_job["status"], "failed")
            # file2 was never attempted -- still sitting exactly where it
            # started, and still recorded as remaining for a future retry.
            self.assertTrue(file2.exists())
            self.assertEqual(move_job["remaining_files"], [str(file2)])

    def test_list_files_excludes_already_moved_files_mid_job(self) -> None:
        # Direct regression test for the live-confirmed bug: the old
        # synchronous move_files() only trimmed entry["files"] once, at the
        # very end of its whole (often very slow) batch, so reopening the
        # file picker while an earlier move was still running re-offered
        # files that had already been relocated -- reselecting them produced
        # a "(2)"-suffixed duplicate at the destination.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            file1, file2 = watch / "one.bin", watch / "two.bin"
            file1.write_bytes(b"one")
            file2.write_bytes(b"two")
            manager, _ = _completed_torrent_manager(root, FakeRpc(), {"a": [file1, file2]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"
            manager.move_files(entry["id"], [str(file1), str(file2)], str(dest), cleanup=False)
            manager._move_tick()  # moves file1 only
            result = manager.list_files(entry["id"])
            names = [f["name"] for f in result["files"]]
            self.assertEqual(names, ["two.bin"])

    def test_restart_mid_move_resumes_from_checkpoint_and_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            file1, file2 = watch / "one.bin", watch / "two.bin"
            file1.write_bytes(b"one")
            file2.write_bytes(b"two")
            manager, _ = _completed_torrent_manager(root, FakeRpc(), {"a": [file1, file2]})
            entry_id = manager.snapshot()["torrents"][0]["id"]
            dest = root / "moved"
            manager.move_files(entry_id, [str(file1), str(file2)], str(dest), cleanup=False)
            manager._move_tick()  # moves file1; file2 still queued -- simulates a live drone mid-job
            with manager._lock:
                move_job_before = dict(manager._torrents[entry_id]["move_job"])
            self.assertEqual(move_job_before["remaining_files"], [str(file2)])

            # Simulate a Drone process restart: a brand new manager sharing
            # the same state DB, never having run a single tick of its own.
            restarted = TorrentManager(_build_settings(root), start_worker=False)
            with restarted._lock:
                restarted_job = restarted._torrents[entry_id]["move_job"]
            self.assertTrue(restarted_job["interrupted"])
            self.assertEqual(restarted_job["remaining_files"], [str(file2)])

            with mock.patch.object(torrent_manager, "_notifications") as fake_notifications:
                restarted._move_tick()
                fired = [call.args[1] for call in fake_notifications.record_event.call_args_list]
            # file2 was the only file left, so this single tick both resumes
            # and (since remaining_files goes empty right after) finishes
            # the job -- both notifications fire, in order.
            self.assertEqual(fired, ["torrent_move_resuming", "torrent_move_finished"])
            with restarted._lock:
                final_job = restarted._torrents[entry_id]["move_job"]
            self.assertEqual(final_job["status"], "complete")
            self.assertEqual(len(final_job["moved_files"]), 2)
            self.assertTrue((dest / "one.bin").exists())
            self.assertTrue((dest / "two.bin").exists())

    def test_resume_overwrites_partial_leftover_of_the_interrupted_file_only(self) -> None:
        # A crash mid-copy of the *current* file can leave a truncated
        # write at its target (cross-filesystem shutil.move is copy-then-
        # delete, not atomic). Resuming must overwrite that specific
        # leftover directly, not run it through the normal collision-suffix
        # loop (which would otherwise mistake it for a real, separate file
        # and produce a spurious "(2)" duplicate instead of replacing it).
        # A second, untouched remaining file must still collision-suffix
        # normally against something unrelated already at its target.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            watch = root / "watch"
            watch.mkdir(parents=True)
            crashed_file = watch / "crashed.bin"
            untouched_file = watch / "untouched.bin"
            crashed_file.write_bytes(b"the real full content")
            untouched_file.write_bytes(b"real")
            manager, _ = _completed_torrent_manager(
                root, FakeRpc(), {"a": [crashed_file, untouched_file]}
            )
            entry_id = manager.snapshot()["torrents"][0]["id"]
            dest = root / "moved"
            dest.mkdir(parents=True)
            # A truncated leftover at crashed.bin's target, as if a prior
            # attempt died partway through writing it.
            (dest / "crashed.bin").write_bytes(b"trunc")
            # An unrelated pre-existing file at untouched.bin's target --
            # this one must still collision-suffix normally.
            (dest / "untouched.bin").write_bytes(b"pre-existing, unrelated")

            manager.move_files(entry_id, [str(crashed_file), str(untouched_file)], str(dest), cleanup=False)
            with manager._lock:
                move_job = manager._torrents[entry_id]["move_job"]
                # Hand-simulate _restore_state()'s interrupted-mid-copy marking
                # without a real process restart: current_file == remaining[0].
                move_job["current_file"] = str(crashed_file)
                move_job["interrupted"] = True
                manager._persist_locked()

            _drain_move_jobs(manager)

            self.assertEqual((dest / "crashed.bin").read_bytes(), b"the real full content")
            self.assertFalse((dest / "crashed (2).bin").exists())
            # untouched.bin's unrelated pre-existing file is left alone; the
            # real one lands alongside it with a collision suffix.
            self.assertEqual((dest / "untouched.bin").read_bytes(), b"pre-existing, unrelated")
            self.assertEqual((dest / "untouched (2).bin").read_bytes(), b"real")

    def test_move_cleanup_is_not_undone_by_a_concurrent_watch_folder_rescan(self) -> None:
        # Issue #40: the payload moves and the entry is dropped, but the poll
        # thread's watch-folder rescan runs in the window before the .torrent
        # file is unlinked and re-registers it as a fresh "queued" torrent, so
        # the torrent stays stuck in the list even though its files are gone.
        # The unlink now happens under the lock, atomically with dropping the
        # entry, so the rescan can never see an orphaned .torrent file.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            watch = root / "watch"
            subfolder = watch / "pack"
            subfolder.mkdir(parents=True)
            file1 = subfolder / "one.bin"
            file1.write_bytes(b"one")
            manager, _ = _completed_torrent_manager(root, rpc, {"pack": [file1]})
            entry = manager.snapshot()["torrents"][0]
            dest = root / "moved"

            original_remove = manager._remove_from_aria2

            def racing_remove(gid, *args, **kwargs):
                # Stand in for the 3s poll thread grabbing the lock in the gap
                # between the entry being dropped and aria2 confirming removal.
                with manager._lock:
                    manager._scan_watch_directory_locked(dict(manager._config))
                return original_remove(gid, *args, **kwargs)

            manager._remove_from_aria2 = racing_remove

            result = manager.move_files(entry["id"], [str(file1)], str(dest), cleanup=True)
            self.assertEqual(result["status"], "queued")
            _drain_move_jobs(manager)

            self.assertEqual(manager.snapshot()["torrents"], [])
            self.assertFalse((watch / "pack.torrent").exists())

    def test_move_cleanup_does_not_readopt_torrent_when_aria2_removal_is_pending(self) -> None:
        # Issue #40: if aria2 is too busy to confirm the removal right away,
        # the move+cleanup path used to drop the return value on the floor --
        # the still-registered gid was then re-adopted by _adopt_orphaned_gids
        # on the next tick and the just-cleaned torrent came back.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            watch = root / "watch"
            subfolder = watch / "pack"
            subfolder.mkdir(parents=True)
            file1 = subfolder / "one.bin"
            file1.write_bytes(b"one")
            manager, _ = _completed_torrent_manager(root, rpc, {"pack": [file1]})
            entry = manager.snapshot()["torrents"][0]
            with manager._lock:
                gid = manager._torrents[entry["id"]]["gid"]
            dest = root / "moved"

            rpc.remove_error = "aria2 is busy writing to a slow disk"
            rpc.active = [
                {
                    "gid": gid,
                    "status": "active",
                    "totalLength": "3",
                    "completedLength": "3",
                    "dir": str(subfolder),
                }
            ]

            manager.move_files(entry["id"], [str(file1)], str(dest), cleanup=True)
            _drain_move_jobs(manager)

            self.assertEqual(manager.snapshot()["torrents"], [])
            with manager._lock:
                self.assertIn(gid, manager._pending_removal_gids)

            # A poll tick while the removal is still pending must not resurrect
            # the torrent as an "Adopted download".
            manager._tick()
            self.assertEqual(manager.snapshot()["torrents"], [])

            # Once aria2 answers, the pending removal drains and nothing is left.
            rpc.remove_error = None
            rpc.active = []
            manager._tick()
            self.assertEqual(manager.snapshot()["torrents"], [])
            with manager._lock:
                self.assertEqual(manager._pending_removal_gids, [])


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

    def test_resume_clears_flag_and_unpauses_previously_active_gid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = TorrentManager(_build_settings(root), start_worker=False)
            manager._daemon = FakeDaemon(rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            _write_torrent(watch, "a")
            manager._tick()
            manager.pause()
            rpc.calls.clear()
            snapshot = manager.resume()
            self.assertFalse(snapshot["paused"])
            # No aria2.unpauseAll -- resume() targets only the gid(s) already
            # holding an active slot (see the concurrency-limit regression
            # test below for why a blanket unpauseAll is wrong here).
            self.assertEqual(len(rpc.method_calls("aria2.unpauseAll")), 0)
            self.assertEqual(len(rpc.method_calls("aria2.unpause")), 1)
            statuses = [entry["status"] for entry in manager.snapshot()["torrents"]]
            self.assertEqual(statuses, ["downloading"])

    def test_resume_does_not_exceed_max_concurrent_downloads(self) -> None:
        # Regression: resume() used to call aria2.unpauseAll, which wakes
        # every paused gid at once -- including queued-but-added-paused
        # torrents that pause()'s aria2.pauseAll swept up alongside the
        # genuinely active ones -- so pausing then resuming started every
        # torrent instead of respecting max_concurrent_downloads.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = TorrentManager(_build_settings(root), start_worker=False)
            manager._daemon = FakeDaemon(rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 2})
            for name in ("a", "b", "c"):
                _write_torrent(watch, name)
            manager._tick()
            statuses = [entry["status"] for entry in manager.snapshot()["torrents"]]
            self.assertEqual(statuses.count("downloading"), 2)
            self.assertEqual(statuses.count("queued"), 1)

            manager.pause()
            rpc.calls.clear()
            snapshot = manager.resume()
            self.assertFalse(snapshot["paused"])
            self.assertEqual(len(rpc.method_calls("aria2.unpauseAll")), 0)
            # Only the two gids already downloading get unpaused directly --
            # the still-queued third does not jump the scheduler.
            self.assertEqual(len(rpc.method_calls("aria2.unpause")), 2)
            statuses = [entry["status"] for entry in manager.snapshot()["torrents"]]
            self.assertEqual(statuses.count("downloading"), 2)
            self.assertEqual(statuses.count("queued"), 1)

            # A further tick still respects the limit -- the queued entry
            # doesn't get started until an active slot actually frees up.
            manager._tick()
            statuses = [entry["status"] for entry in manager.snapshot()["torrents"]]
            self.assertEqual(statuses.count("downloading"), 2)
            self.assertEqual(statuses.count("queued"), 1)

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


class TorrentsGridMobileCssTests(unittest.TestCase):
    """table.bff-stack (present on the torrents table, see
    renderTorrentTableShell) turns each row into a stacked multi-line
    label:value card on phone widths, auto-labeled from the <thead> via
    decorateStackTables(). .torrents-table's own fixed 2.5rem row height plus
    nowrap/ellipsis -- needed on desktop so the 3s auto-refresh poll never
    reflows the grid mid-click -- used to apply unconditionally, clipping and
    overlapping those stacked lines on top of each other on mobile. Regression
    test: those rules must stay scoped to desktop/tablet widths only."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.css = root.joinpath("app/web/static/css/drone.css").read_text(encoding="utf-8")
        cls.js = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")

    def test_fixed_row_height_and_nowrap_are_scoped_to_desktop_only(self) -> None:
        self.assertIn(
            "@media (min-width: 768px) {\n      .torrents-table {\n        table-layout: fixed;", self.css
        )
        self.assertIn("height: 2.5rem;", self.css)
        self.assertIn("white-space: nowrap;", self.css)

    def test_torrents_table_still_carries_bff_stack_for_mobile(self) -> None:
        self.assertIn("bff-stack torrents-table", self.js)


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


class PartialTorrentMigrationTests(unittest.TestCase):
    def _partial_manager(self, root: Path):
        rpc = FakeRpc()
        manager = TorrentManager(_build_settings(root), start_worker=False)
        manager._daemon = FakeDaemon(rpc)
        watch = root / "watch"
        old_downloads = root / "old"
        new_downloads = root / "new"
        manager.update_settings({"directory": str(watch), "download_directory": str(old_downloads)})
        _write_torrent(watch, "game")
        manager._tick()
        entry = manager.snapshot()["torrents"][0]
        payload = old_downloads / "game.bin"
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(b"partial payload")
        sidecar = old_downloads / "game.bin.aria2"
        sidecar.write_bytes(b"resume state")
        with manager._lock:
            live = manager._torrents[entry["id"]]
            live["files"] = [str(payload)]
            live["completed_bytes"] = 7
            live["total_bytes"] = 20
            live["progress_percent"] = 35.0
            manager._persist_locked()
        manager.update_settings({"download_directory": str(new_downloads)})
        return manager, rpc, entry["id"], payload, sidecar, new_downloads

    def test_stages_payload_and_resume_sidecar_verifies_then_removes_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, rpc, entry_id, payload, sidecar, destination = self._partial_manager(Path(tmp))
            with manager._lock:
                old_gid = manager._torrents[entry_id]["gid"]

            result = manager.migrate_partial(entry_id)
            self.assertEqual(result["status"], "queued")
            manager._migration_tick()
            manager._migration_tick()

            # Staging is deliberately non-destructive until aria2 proves the
            # destination sidecar retained the prior progress.
            self.assertTrue(payload.exists())
            self.assertTrue(sidecar.exists())
            self.assertEqual((destination / payload.name).read_bytes(), b"partial payload")
            self.assertEqual((destination / sidecar.name).read_bytes(), b"resume state")
            with manager._lock:
                migrated = manager._torrents[entry_id]
                self.assertEqual(migrated["download_dir"], str(destination.resolve()))
                self.assertEqual(migrated["migration_job"]["status"], "verifying")
                self.assertIsNone(migrated["gid"])
            self.assertIn(old_gid, [params[0] for params in rpc.method_calls("aria2.forceRemove")])

            manager._tick()
            adds = rpc.method_calls("aria2.addTorrent")
            self.assertEqual(adds[-1][2]["dir"], str(destination.resolve()))
            with manager._lock:
                new_gid = manager._torrents[entry_id]["gid"]
            self.assertNotIn(new_gid, [params[0] for params in rpc.method_calls("aria2.unpause")])
            manager._migration_tick()
            with manager._lock:
                self.assertEqual(manager._torrents[entry_id]["migration_job"]["status"], "verifying")
            rpc.statuses[new_gid].update({"totalLength": "20", "completedLength": "7", "infoHash": "hash"})
            manager._tick()
            manager._migration_tick()
            manager._migration_tick()
            manager._migration_tick()
            manager._migration_tick()

            self.assertFalse(payload.exists())
            self.assertFalse(sidecar.exists())
            with manager._lock:
                self.assertEqual(manager._torrents[entry_id]["migration_job"]["status"], "complete")

    def test_rejects_destination_conflict_without_stopping_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, rpc, entry_id, payload, _sidecar, destination = self._partial_manager(Path(tmp))
            destination.mkdir(parents=True, exist_ok=True)
            (destination / payload.name).write_bytes(b"existing")

            result = manager.migrate_partial(entry_id)

            self.assertEqual(result["status"], "destination_conflict")
            self.assertEqual(rpc.method_calls("aria2.forceRemove"), [])
            self.assertTrue(payload.exists())

    def test_zero_counter_can_migrate_when_resume_files_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _rpc, entry_id, _payload, _sidecar, _destination = self._partial_manager(Path(tmp))
            with manager._lock:
                manager._torrents[entry_id]["completed_bytes"] = 0
            result = manager.migrate_partial(entry_id)
            self.assertEqual(result["status"], "queued")

    def test_failed_move_stays_stopped_until_user_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, rpc, entry_id, _payload, _sidecar, _destination = self._partial_manager(Path(tmp))
            self.assertEqual(manager.migrate_partial(entry_id)["status"], "queued")
            with mock.patch.object(torrent_manager, "_copy_migration_file", side_effect=OSError("drive unavailable")):
                manager._migration_tick()
            with manager._lock:
                self.assertEqual(manager._torrents[entry_id]["migration_job"]["status"], "failed")

            add_count = len(rpc.method_calls("aria2.addTorrent"))
            manager._tick()
            self.assertEqual(len(rpc.method_calls("aria2.addTorrent")), add_count)
            self.assertEqual(manager.retry_partial_migration(entry_id)["status"], "queued")

    def test_crash_during_copy_discards_partial_target_and_recopies_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _rpc, entry_id, payload, _sidecar, _destination = self._partial_manager(Path(tmp))
            self.assertEqual(manager.migrate_partial(entry_id)["status"], "queued")
            with manager._lock:
                job = manager._torrents[entry_id]["migration_job"]
                item = job["remaining_files"][0]
                job["gid"] = None
                job["status"] = "queued"
                job["current_source"] = item["source"]
                manager._torrents[entry_id]["gid"] = None
                manager._persist_locked()
            target = Path(item["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"truncated")

            manager._migration_tick()

            self.assertEqual(target.read_bytes(), b"partial payload")
            self.assertEqual(payload.read_bytes(), b"partial payload")
            with manager._lock:
                self.assertEqual(len(manager._torrents[entry_id]["migration_job"]["moved_files"]), 1)

    def test_resume_verification_failure_keeps_original_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, rpc, entry_id, payload, sidecar, destination = self._partial_manager(Path(tmp))
            self.assertEqual(manager.migrate_partial(entry_id)["status"], "queued")
            manager._migration_tick()
            manager._migration_tick()
            manager._tick()
            with manager._lock:
                new_gid = manager._torrents[entry_id]["gid"]
            rpc.statuses[new_gid].update({"totalLength": "20", "completedLength": "0", "infoHash": "hash"})
            for _ in range(3):
                manager._tick()
                manager._migration_tick()

            self.assertTrue(payload.exists())
            self.assertTrue(sidecar.exists())
            self.assertTrue((destination / payload.name).exists())
            with manager._lock:
                entry = manager._torrents[entry_id]
                self.assertEqual(entry["download_dir"], str(payload.parent.resolve()))
                self.assertEqual(entry["migration_job"]["status"], "failed")
                self.assertIn("lost progress", entry["migration_job"]["error"])

            retry_as_new = manager.migrate_partial(entry_id)
            self.assertEqual(retry_as_new["status"], "not_applicable")
            self.assertIn("retry", retry_as_new["message"])

    def test_verification_phase_rejects_a_second_migration_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _rpc, entry_id, payload, sidecar, _destination = self._partial_manager(Path(tmp))
            self.assertEqual(manager.migrate_partial(entry_id)["status"], "queued")
            manager._migration_tick()
            manager._migration_tick()

            result = manager.migrate_partial(entry_id)

            self.assertEqual(result["status"], "already_in_progress")
            self.assertEqual(result["migration_job"]["status"], "verifying")
            self.assertTrue(payload.exists())
            self.assertTrue(sidecar.exists())

    def test_verification_resumes_after_manager_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, _rpc, entry_id, payload, sidecar, destination = self._partial_manager(root)
            self.assertEqual(manager.migrate_partial(entry_id)["status"], "queued")
            manager._migration_tick()
            manager._migration_tick()

            restarted_rpc = FakeRpc()
            restarted = TorrentManager(_build_settings(root), start_worker=False)
            restarted._daemon = FakeDaemon(restarted_rpc)
            with restarted._lock:
                restored = restarted._torrents[entry_id]
                self.assertEqual(restored["migration_job"]["status"], "verifying")
                self.assertIsNone(restored["gid"])

            restarted._tick()
            with restarted._lock:
                new_gid = restarted._torrents[entry_id]["gid"]
            restarted_rpc.statuses[new_gid].update({"totalLength": "20", "completedLength": "7", "infoHash": "hash"})
            restarted._tick()
            restarted._migration_tick()
            restarted._migration_tick()
            restarted._migration_tick()
            restarted._migration_tick()

            self.assertFalse(payload.exists())
            self.assertFalse(sidecar.exists())
            self.assertEqual((destination / payload.name).read_bytes(), b"partial payload")
            with restarted._lock:
                self.assertEqual(restarted._torrents[entry_id]["migration_job"]["status"], "complete")

    def test_zero_counter_does_not_silently_retarget_existing_resume_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _rpc, entry_id, payload, _sidecar, destination = self._partial_manager(Path(tmp))
            with manager._lock:
                entry = manager._torrents[entry_id]
                entry["completed_bytes"] = 0
                entry["status"] = "queued"
                entry["download_dir"] = str(payload.parent.resolve())
                manager._refresh_pending_download_dirs_locked(dict(manager._config))
                self.assertEqual(entry["download_dir"], str(payload.parent.resolve()))
                self.assertNotEqual(entry["download_dir"], str(destination.resolve()))
            self.assertEqual(manager.force_start(entry_id)["status"], "ok")
            with manager._lock:
                self.assertEqual(manager._torrents[entry_id]["download_dir"], str(payload.parent.resolve()))

    def test_migration_includes_unreported_files_inside_torrent_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _rpc, entry_id, _payload, _sidecar, destination = self._partial_manager(Path(tmp))
            with manager._lock:
                source_dir = Path(manager._torrents[entry_id]["download_dir"])
            unreported = source_dir / "game" / "nested" / "unreported.bin"
            unreported.parent.mkdir(parents=True)
            unreported.write_bytes(b"also required")

            self.assertEqual(manager.migrate_partial(entry_id)["status"], "queued")
            with manager._lock:
                job = manager._torrents[entry_id]["migration_job"]
                mappings = {item["source"]: item["target"] for item in job["remaining_files"]}
            self.assertEqual(
                mappings[str(unreported.resolve())],
                str(destination.resolve() / "game" / "nested" / "unreported.bin"),
            )


class TorrentSettingsHandlerTests(unittest.TestCase):
    """The watched folder ("directory") is no longer settable through the
    admin HTTP API -- only TorrentManager.update_settings() itself (used
    directly by tests/internal callers) still accepts it."""

    def test_settings_update_strips_directory_from_the_payload(self) -> None:
        from app.web import handlers_torrents

        class _FakeManager:
            def __init__(self) -> None:
                self.received_payload = None

            def update_settings(self, payload):
                self.received_payload = payload
                return {"directory": "/fixed/torrents", "max_concurrent_downloads": payload.get("max_concurrent_downloads", 3)}

        class _FakeHandler(handlers_torrents.HandlersTorrentsMixin):
            def __init__(self) -> None:
                self.response = None

            def _send_json(self, status_code, payload):
                self.response = (status_code, payload)

        fake_manager = _FakeManager()
        handler = _FakeHandler()
        with mock.patch.object(handlers_torrents, "_get_torrent_manager", return_value=fake_manager):
            handler._handle_admin_torrents_settings_update({"directory": "/some/other/path", "max_concurrent_downloads": 5})

        self.assertNotIn("directory", fake_manager.received_payload)
        self.assertEqual(fake_manager.received_payload["max_concurrent_downloads"], 5)
        self.assertEqual(handler.response[0], 200)
        self.assertEqual(handler.response[1]["settings"]["directory"], "/fixed/torrents")


if __name__ == "__main__":
    unittest.main()
