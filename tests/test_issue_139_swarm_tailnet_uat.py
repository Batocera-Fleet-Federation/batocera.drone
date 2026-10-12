"""User-level acceptance tests for issue #139.

Swarm page stalls, tailnet-only peers show Offline, and tailnet auto-enrollment
never retries. Covers:

* the Swarm self card is built from the SQLite cache and never walks the ROM
  tree, even while a metadata scan is in progress;
* a Drone that starts logged out keeps retrying the swarm bootstrap;
* an unreachable peer is explained when this Drone is off the tailnet;
* the download queue payload can be compact/capped, queue persistence is
  coalesced for bulk enqueues, and one bulk sync request is capped;
* the browser client times out and surfaces the new states.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import app.drone_api as drone_api
from app.storage import rom_metadata_store as store
from app.transfer import peer_workers
from app.web import handlers_network
from app.web.handlers_network import HandlersNetworkMixin


ROOT = Path(__file__).resolve().parents[1]


def _settings(root: Path):
    env = {
        "USERDATA_ROOT": str(root / "userdata"),
        "ROMS_ROOT": str(root / "userdata" / "roms"),
        "BIOS_ROOT": str(root / "userdata" / "bios"),
        "SAVES_ROOT": str(root / "userdata" / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.db"),
        "DRONE_DEVICE_ID": "self-drone",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return drone_api.Settings.from_env()


def _seed_cache(settings, systems, scan_in_progress):
    with store._open_state_database(store._state_database_path(settings.userdata_root)) as connection:
        store._ensure_schema(connection)
        for key, value in (("last_full_scan_at", "2026-10-01T00:00:00+00:00"), ("scan_in_progress", scan_in_progress)):
            connection.execute(
                "INSERT INTO cache_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
        connection.executemany("INSERT INTO asset_systems (name, rom_count) VALUES (?, ?)", systems)


class SwarmSelfEntryDoesNotWalkDiskTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = _settings(Path(self._tmp.name))
        self.settings.roms_root.mkdir(parents=True, exist_ok=True)
        (self.settings.roms_root / "snes").mkdir()
        self.repo = drone_api.RomRepository(self.settings.roms_root, self.settings.bios_root)

    def test_cached_systems_are_served_while_a_scan_is_in_progress(self):
        _seed_cache(self.settings, [("snes", 12), ("nes", 3)], scan_in_progress=True)
        # The legacy path hides the counts during a scan (and then walks the disk)...
        self.assertIsNone(store._read_sqlite_asset_systems(self.settings.userdata_root))
        # ...the cache-only path does not.
        with mock.patch.object(self.repo, "_count_rom_items", side_effect=AssertionError("walked the disk")), \
                mock.patch.object(self.repo, "list_local_system_names", side_effect=AssertionError("walked the disk")):
            rows = self.repo.list_cached_systems()
        self.assertEqual({row["name"]: row["rom_count"] for row in rows}, {"snes": 12, "nes": 3})

    def test_cached_systems_are_empty_before_the_first_scan(self):
        self.assertEqual(self.repo.list_cached_systems(), [])

    def test_self_summary_uses_cache_only_during_a_scan(self):
        _seed_cache(self.settings, [("snes", 12), ("nes", 3)], scan_in_progress=True)
        handler = SimpleNamespace(settings=self.settings, repository=self.repo)
        with mock.patch.object(self.repo, "list_systems", side_effect=AssertionError("full listing")), \
                mock.patch.object(self.repo, "_count_rom_items", side_effect=AssertionError("walked the disk")):
            summary = HandlersNetworkMixin._swarm_self_summary(handler)
        self.assertEqual(summary["systems"], ["nes", "snes"])
        self.assertEqual(summary["system_counts"], {"snes": 12, "nes": 3})
        self.assertEqual(summary["counts"]["systems"], 2)
        self.assertEqual(summary["counts"]["roms"], 0)  # rom rows are separate from per-system counts

    def test_self_entry_does_not_collect_the_peer_inventory(self):
        _seed_cache(self.settings, [("snes", 12)], scan_in_progress=True)
        handler = SimpleNamespace(settings=self.settings, repository=self.repo)
        handler._swarm_self_summary = lambda: HandlersNetworkMixin._swarm_self_summary(handler)
        handler._collect_peer_inventory = mock.Mock(side_effect=AssertionError("slow inventory path"))
        with mock.patch.object(handlers_network._local_network, "discovery_payload", return_value={"hostname": "h"}):
            entry = HandlersNetworkMixin._swarm_self_entry(handler)
        self.assertTrue(entry["is_self"])
        self.assertEqual(entry["summary"]["system_counts"], {"snes": 12})


class TailnetBootstrapRetryTests(unittest.TestCase):
    def _retry_target(self):
        targets = {}

        def thread(*args, **kwargs):
            targets[kwargs["name"]] = kwargs["target"]
            return mock.Mock()

        with mock.patch.object(peer_workers, "Thread", side_effect=thread), \
                mock.patch.object(peer_workers._local_network, "start_discovery_worker", side_effect=RuntimeError("stop setup")):
            with self.assertRaisesRegex(RuntimeError, "stop setup"):
                peer_workers._start_local_network_workers(mock.Mock())
        return targets["drone-tailnet-bootstrap-retry"]

    def _run_one_pass(self, status, bootstrap=None):
        bootstrap = bootstrap or mock.Mock(return_value=False)
        with mock.patch.object(peer_workers.time, "sleep", side_effect=[None, RuntimeError("stop loop")]), \
                mock.patch.object(peer_workers, "tailnet_status", return_value=status), \
                mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm", bootstrap):
            with self.assertRaisesRegex(RuntimeError, "stop loop"):
                self._retry_target()()
        return bootstrap

    def test_not_enrolled_drone_retries_the_swarm_bootstrap(self):
        bootstrap = self._run_one_pass({"installed": True, "running": True, "enrolled": False})
        bootstrap.assert_called_once()

    def test_enrolled_drone_does_not_bootstrap(self):
        self._run_one_pass({"installed": True, "running": True, "enrolled": True}).assert_not_called()

    def test_drone_without_tailscale_does_not_bootstrap(self):
        self._run_one_pass({"installed": False, "running": False, "enrolled": False}).assert_not_called()

    def test_bootstrap_failure_does_not_kill_the_retry_loop(self):
        bootstrap = mock.Mock(side_effect=OSError("peer exploded"))
        with mock.patch.object(peer_workers.time, "sleep", side_effect=[None, None, RuntimeError("stop loop")]), \
                mock.patch.object(peer_workers, "tailnet_status", return_value={"installed": True, "enrolled": False}), \
                mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm", bootstrap):
            with self.assertRaisesRegex(RuntimeError, "stop loop"):
                self._retry_target()()
        self.assertEqual(bootstrap.call_count, 2)

    def test_interval_is_configurable_with_a_floor(self):
        sleeps = []
        with mock.patch.dict("os.environ", {"DRONE_TAILNET_BOOTSTRAP_INTERVAL_SECONDS": "5"}), \
                mock.patch.object(peer_workers.time, "sleep", side_effect=lambda s: (sleeps.append(s), (_ for _ in ()).throw(RuntimeError("stop")))[0]):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                self._retry_target()()
        self.assertEqual(sleeps, [60])


class OfflineReasonTests(unittest.TestCase):
    def test_tailnet_peer_is_explained_when_this_drone_is_off_the_tailnet(self):
        with mock.patch.object(handlers_network, "tailnet_status", return_value={"installed": True, "enrolled": False}):
            reason = HandlersNetworkMixin._swarm_offline_reason({"tailnet_ip": "100.64.0.9"})
        self.assertEqual(reason["offline_reason"], "tailnet_not_connected")
        self.assertIn("not connected to the tailnet", reason["offline_hint"])

    def test_no_reason_when_this_drone_is_on_the_tailnet_or_peer_has_no_tailnet_address(self):
        with mock.patch.object(handlers_network, "tailnet_status", return_value={"installed": True, "enrolled": True}):
            self.assertEqual(HandlersNetworkMixin._swarm_offline_reason({"tailnet_ip": "100.64.0.9"}), {})
        with mock.patch.object(handlers_network, "tailnet_status", side_effect=AssertionError("not needed")):
            self.assertEqual(HandlersNetworkMixin._swarm_offline_reason({"tailnet_ip": ""}), {})

    def test_probe_of_unreachable_tailnet_peer_carries_the_reason(self):
        handler = SimpleNamespace(settings=mock.Mock(use_fake_data=False), _swarm_peer_ui_url=lambda peer: "")
        peer = {"drone_id": "r5", "name": "BATOCERA-R5", "tailnet_ip": "100.64.0.9"}
        with mock.patch.object(handlers_network, "_peer_get_json_for_peer", side_effect=OSError("timed out")), \
                mock.patch.object(handlers_network, "tailnet_status", return_value={"installed": True, "enrolled": False}):
            entry = HandlersNetworkMixin._swarm_probe_peer(handler, peer)
        self.assertFalse(entry["online"])
        self.assertEqual(entry["offline_reason"], "tailnet_not_connected")


class DownloadQueuePayloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = _settings(Path(self._tmp.name))
        repo = drone_api.RomRepository(self.settings.roms_root, self.settings.bios_root)
        with mock.patch("app.drone_api.Thread.start"):
            self.manager = drone_api.DownloadManager(self.settings, repo)

    def _add_queued(self, count):
        with self.manager._lock:
            for index in range(count):
                job_id = f"job-{index}"
                self.manager._jobs[job_id] = {
                    "id": job_id, "job_id": job_id, "status": "queued", "total_bytes": 100, "downloaded_bytes": 0,
                }

    def test_default_snapshot_keeps_the_legacy_shape(self):
        self._add_queued(3)
        snapshot = self.manager.snapshot()
        self.assertEqual(len(snapshot["downloads"]), 3)
        self.assertEqual(snapshot["queued_total"], 3)
        self.assertFalse(snapshot["queued_truncated"])

    def test_compact_snapshot_does_not_list_the_queue_twice(self):
        self._add_queued(3)
        self.assertNotIn("downloads", self.manager.snapshot(compact=True))

    def test_queued_limit_caps_rows_but_not_the_totals_or_estimate(self):
        self._add_queued(10)
        full = self.manager.snapshot()
        capped = self.manager.snapshot(compact=True, queued_limit=4)
        self.assertEqual([job["job_id"] for job in capped["queued"]], ["job-0", "job-1", "job-2", "job-3"])
        self.assertEqual(capped["queued_total"], 10)
        self.assertTrue(capped["queued_truncated"])
        self.assertEqual(capped["queue_remaining_bytes"], full["queue_remaining_bytes"])

    def test_batch_persistence_writes_the_queue_once(self):
        from app.transfer import download_manager
        with mock.patch.object(download_manager, "_save_state_payload") as save:
            with self.manager.batch_persistence():
                with self.manager._lock:
                    for _ in range(50):
                        self.manager._persist_state_locked()
            self.assertEqual(save.call_count, 1)
            with self.manager._lock:
                self.manager._persist_state_locked()
            self.assertEqual(save.call_count, 2)

    def test_batch_persistence_does_not_write_when_nothing_changed(self):
        from app.transfer import download_manager
        with mock.patch.object(download_manager, "_save_state_payload") as save:
            with self.manager.batch_persistence():
                pass
            save.assert_not_called()

    def test_admin_endpoint_passes_compact_and_limit_through(self):
        from app.web.handlers_downloads import HandlersDownloadsMixin
        manager = mock.Mock()
        handler = SimpleNamespace(settings=self.settings, _send_json=mock.Mock())
        with mock.patch("app.web.handlers_downloads._get_download_manager", create=True, return_value=manager):
            HandlersDownloadsMixin._handle_admin_downloads(handler, {"compact": ["1"], "queued_limit": ["25"]})
        manager.snapshot.assert_called_once_with(compact=True, queued_limit=25)


class BulkSyncCapTests(unittest.TestCase):
    def test_one_request_cannot_enqueue_more_than_the_cap(self):
        items = [{"file_path": f"game{i}.zip", "file_size": 10} for i in range(20)]
        handler = object.__new__(drone_api.RomRequestHandler)
        handler.settings = mock.Mock(device_id="target-a")
        handler.repository = mock.Mock()
        handler._fetch_peer_inventory = mock.Mock(return_value={"items": items, "total": len(items)})
        handler._enqueue_local_asset = mock.Mock(
            side_effect=lambda manager, config, peer, asset_type, item, **kw: [
                {"job_id": f"j-{item['file_path']}", "file_type": "ROM", "total_bytes": 10, "status": "queued"}
            ]
        )
        handler._send_json = mock.Mock()
        with mock.patch.object(handlers_network._local_network, "is_local_mode", return_value=True), \
                mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value={"drone_id": "peer-a"}), \
                mock.patch.object(handlers_network, "_get_download_manager", return_value=mock.Mock()), \
                mock.patch.object(handlers_network, "SYNC_BULK_MAX_JOBS", 5):
            handler._handle_admin_local_sync_bulk({
                "peer_id": "peer-a", "asset_type": "roms", "system": "snes",
                "include_artwork": False, "include_roms": True,
            })
        status_code, payload = handler._send_json.call_args.args
        self.assertEqual(status_code, 202)
        self.assertEqual(payload["queued_assets"], 5)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["max_jobs"], 5)


class ClientWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = ROOT.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")

    def test_api_helper_has_a_timeout_with_a_clear_message(self):
        start = self.js.index("async function api(url")
        body = self.js[start:self.js.index("async function apiPost(", start)]
        self.assertIn("AbortController", body)
        self.assertIn("did not answer within", body)

    def test_swarm_first_paint_request_fails_fast(self):
        self.assertIn('api("/admin/swarm/overview?probe=0", { timeoutMs: 20000 })', self.js)

    def test_transfers_poll_uses_the_compact_capped_queue(self):
        self.assertIn('TRANSFERS_POLL_PATH = "/admin/downloads?compact=1&queued_limit=500"', self.js)
        self.assertEqual(self.js.count("api(TRANSFERS_POLL_PATH)"), 2)

    def test_offline_tailnet_peer_shows_the_reason(self):
        self.assertIn('drone.offline_reason === "tailnet_not_connected"', self.js)
        self.assertIn("drone.offline_hint", self.js)


if __name__ == "__main__":
    unittest.main()
