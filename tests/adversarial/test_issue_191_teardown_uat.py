"""Issue #191 UAT: discovery must not write a peer map after the temp DB is gone.

The original failure was a real filesystem path (not a #170 mock): pytest
teardown removed the temp userdata tree while ``drone-local-discovery`` was
still in ``_save_peer_map`` -> ``open_database`` ``PRAGMA journal_mode=WAL``,
raising ``sqlite3.OperationalError: disk I/O error`` as an unhandled thread
exception.

Expected behavior, from the issue (not from the patch):

- The discovery worker is stoppable. Callers that own the state DB must signal
  stop and join the thread before deleting that DB.
- After stop+join, removing the temp directory must not produce a late WAL
  open, a recreated sqlite file, or an unhandled thread exception.
- A swallow of ``sqlite3.Error`` while the thread keeps looping is not enough;
  the worker has to be dead before cleanup.
- The path under test is a real temporary sqlite file.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app import drone_api
from app.common.settings import Settings
from app.storage import state_store
from app.transfer import local_network, peer_workers


def _free_udp_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


def _discovery_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "drone-local-discovery"]


class _ThreadExceptionGuard:
    def __init__(self) -> None:
        self.failures: list[BaseException] = []
        self._previous = None

    def __enter__(self) -> "_ThreadExceptionGuard":
        self._previous = threading.excepthook

        def hook(args: threading.ExceptHookArgs) -> None:
            if args.exc_value is not None:
                self.failures.append(args.exc_value)
            if self._previous is not None:
                self._previous(args)

        threading.excepthook = hook
        return self

    def __exit__(self, *exc: object) -> None:
        threading.excepthook = self._previous

    def disk_io_failures(self) -> list[BaseException]:
        hits = []
        for error in self.failures:
            text = f"{error.__class__.__name__}: {error}".lower()
            if isinstance(error, sqlite3.Error) or "disk i/o" in text:
                hits.append(error)
        return hits


class DiscoveryTeardownUAT(unittest.TestCase):
    def setUp(self) -> None:
        # Pollers left behind by an earlier harness's create_server() re-resolve the env-selected
        # state DB on every tick; retire them so they cannot write into this test's database (#420).
        drone_api.retire_background_pollers()
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-191-uat-")
        self.root = Path(self._tmp.name)
        self.userdata = self.root / "userdata"
        self.userdata.mkdir()
        self.db_path = self.root / "state.sqlite3"
        self.port = _free_udp_port()
        self._env = mock.patch.dict(
            os.environ,
            {
                "USERDATA_ROOT": str(self.userdata),
                "DRONE_STATE_DATABASE_FILE": str(self.db_path),
                "DRONE_DEVICE_ID": "issue-191-host",
                "HTTPS_PORT": "8443",
                "HTTP_ONLY": "1",
                "ROM_METADATA_POLL_SECONDS": "0",
                "LOG_DIR": str(self.root / "logs"),
                "TLS_SELF_SIGNED_DIR": str(self.userdata / "system" / "certs"),
                "DRONE_LOCAL_HEALTH_INTERVAL_SECONDS": "10",
            },
            clear=False,
        )
        self._env.start()
        self.settings = Settings.from_env()
        self._port_patch = mock.patch.object(local_network, "DISCOVERY_PORT", self.port)
        self._interval_patch = mock.patch.object(local_network, "DISCOVERY_INTERVAL_SECONDS", 3600)
        self._join_patch = mock.patch.object(local_network, "_join_multicast_group")
        self._announce_patch = mock.patch.object(local_network, "announce", return_value=True)
        self._port_patch.start()
        self._interval_patch.start()
        self._join_patch.start()
        self._announce_patch.start()
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self) -> None:
        self._reset_workers()
        self._announce_patch.stop()
        self._join_patch.stop()
        self._interval_patch.stop()
        self._port_patch.stop()
        self._env.stop()

    def _reset_workers(self) -> None:
        peer_workers.stop_local_network_workers(timeout=3.0)
        for thread in _discovery_threads():
            thread.join(timeout=3.0)

    def _send(self, payload: dict | bytes) -> None:
        packet = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(packet, ("127.0.0.1", self.port))

    def _peer_payload(self, drone_id: str = "issue-191-peer") -> dict:
        return {
            "service": local_network.DISCOVERY_SERVICE,
            "kind": "announce",
            "drone_id": drone_id,
            "name": drone_id,
            "scheme": "http",
            "api_port": 8080,
        }

    def _wait_for_peer(self, drone_id: str = "issue-191-peer") -> None:
        def seen() -> bool:
            try:
                peers = state_store.load_payload(self.db_path, "local_discovered_peers", {})
            except sqlite3.OperationalError:
                return False
            return isinstance(peers, dict) and drone_id in peers

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            self._send(self._peer_payload(drone_id))
            if seen():
                return
            time.sleep(0.1)
        self.fail("discovery worker did not persist the injected peer map row")

    def test_stop_and_join_then_delete_real_temp_db_has_no_late_wal_write(self) -> None:
        """The original teardown race: real sqlite path, then rmtree after join."""
        guard = _ThreadExceptionGuard()
        opens_after_stop: list[str] = []
        stop_joined = threading.Event()
        real_open = state_store.open_database

        def spy_open(path, *args, **kwargs):
            if stop_joined.is_set():
                opens_after_stop.append(str(path))
            return real_open(path, *args, **kwargs)

        with guard, mock.patch.object(state_store, "open_database", side_effect=spy_open):
            stop = threading.Event()
            worker = local_network.start_discovery_worker(
                self.settings, lambda: "", stop_event=stop
            )
            self._wait_for_peer()
            self.assertTrue(self.db_path.is_file())
            stop.set()
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
            stop_joined.set()
            self._tmp.cleanup()
            time.sleep(1.5)
            self._send(self._peer_payload("late-peer"))
            time.sleep(1.5)

        self.assertFalse(worker.is_alive())
        self.assertFalse(self.root.exists())
        self.assertEqual(opens_after_stop, [])
        self.assertEqual(guard.disk_io_failures(), [])

    def test_stop_local_network_workers_covers_create_server_style_teardown(self) -> None:
        """``create_server`` starts workers via this helper; teardown must join before rmtree."""
        guard = _ThreadExceptionGuard()
        cert = mock.Mock()
        cert.metadata.return_value = {"fingerprint": ""}
        with guard, \
                mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
                mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"), \
                mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True, "installed": True, "enrolled": True}), \
                mock.patch.object(peer_workers, "_repair_tailnet_data_path"), \
                mock.patch.object(peer_workers, "DroneCertificateManager", return_value=cert):
            peer_workers._start_local_network_workers(self.settings)
            workers = _discovery_threads()
            self.assertTrue(workers)
            self._wait_for_peer()
            peer_workers.stop_local_network_workers(timeout=8.0)
            self.assertEqual(_discovery_threads(), [])
            self._tmp.cleanup()
            time.sleep(1.5)
            self._send(self._peer_payload("after-cleanup"))
            time.sleep(1.5)
        self.assertFalse(self.root.exists())
        self.assertEqual(guard.disk_io_failures(), [])
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)

    def test_in_flight_peer_map_save_is_joined_before_directory_removal(self) -> None:
        """Join must wait out an in-flight ``_save_peer_map`` so rmtree cannot race it."""
        started = threading.Event()
        release = threading.Event()
        real_save = local_network.save_payload

        def blocking_save(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(timeout=8), "test failed to release in-flight save")
            return real_save(*args, **kwargs)

        stop = threading.Event()
        with mock.patch.object(local_network, "save_payload", side_effect=blocking_save):
            worker = local_network.start_discovery_worker(
                self.settings, lambda: "", stop_event=stop
            )
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and not started.is_set():
                self._send(self._peer_payload())
                time.sleep(0.05)
            self.assertTrue(started.is_set(), "worker never entered _save_peer_map")
            stop.set()
            joiner_done = threading.Event()

            def join_worker() -> None:
                worker.join(timeout=8)
                joiner_done.set()

            joiner = threading.Thread(target=join_worker, name="issue-191-join")
            joiner.start()
            time.sleep(0.3)
            self.assertFalse(joiner_done.is_set(), "join returned while _save_peer_map was still blocked")
            self.assertTrue(self.root.exists())
            release.set()
            joiner.join(timeout=8)
            self.assertTrue(joiner_done.is_set())
            self.assertFalse(worker.is_alive())
        self._tmp.cleanup()
        self.assertFalse(self.root.exists())
