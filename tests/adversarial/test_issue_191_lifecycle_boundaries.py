"""Issue #191 boundaries: malformed packets, error paths, and stop API edges.

Derived from the issue before treating the delivery tests as ground truth:

- Stop must work after garbage UDP that is not a peer announcement.
- A ``sqlite3.OperationalError`` during ``_save_peer_map`` must not become an
  unhandled thread exception, and stop+join must still finish.
- ``stop_local_network_workers`` is a no-op when nothing was started, and is
  idempotent.
- An already-set stop event means the worker never writes, so the temp DB can
  be deleted immediately.
- Swallowing disk I/O errors while the thread stays alive is not the fix;
  after stop the discovery thread must be gone.
- After stop, the health loop (same process, same state DB) must not write
  ``local_peer_checks`` into a directory that teardown already removed.
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


def _health_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "drone-local-peer-health"]


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


class LifecycleBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-191-bound-")
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

    def _start_discovery(self, stop: threading.Event | None = None) -> tuple[threading.Event, threading.Thread]:
        event = stop if stop is not None else threading.Event()
        worker = local_network.start_discovery_worker(self.settings, lambda: "", stop_event=event)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if worker.is_alive():
                # Give bind() a tick so the first datagram is not dropped.
                time.sleep(0.05)
                return event, worker
            time.sleep(0.02)
        self.fail("discovery worker thread never started")

    def test_stop_local_network_workers_without_prior_start_is_a_noop(self) -> None:
        peer_workers._LOCAL_NETWORK_STOP = None
        peer_workers._LOCAL_NETWORK_DISCOVERY = None
        peer_workers.stop_local_network_workers()
        peer_workers.stop_local_network_workers(timeout=0.1)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)

    def test_already_set_stop_event_never_writes_then_temp_dir_can_be_removed(self) -> None:
        stop = threading.Event()
        stop.set()
        worker = local_network.start_discovery_worker(self.settings, lambda: "", stop_event=stop)
        worker.join(timeout=8)
        self.assertFalse(worker.is_alive())
        self._tmp.cleanup()
        self.assertFalse(self.root.exists())
        self.assertFalse(self.db_path.exists())

    def test_malformed_udp_does_not_block_stop_join_or_temp_db_removal(self) -> None:
        garbage = (
            b"",
            b"\x00\xff not-json",
            json.dumps({"service": "wrong-service", "drone_id": "x"}).encode("utf-8"),
            json.dumps({"service": local_network.DISCOVERY_SERVICE, "drone_id": ""}).encode("utf-8"),
            json.dumps({"service": local_network.DISCOVERY_SERVICE}).encode("utf-8"),
            json.dumps({"service": local_network.DISCOVERY_SERVICE, "drone_id": None}).encode("utf-8"),
        )
        stop, worker = self._start_discovery()
        for packet in garbage:
            self._send(packet)
        time.sleep(0.4)
        self.assertTrue(worker.is_alive())
        stop.set()
        worker.join(timeout=8)
        self.assertFalse(worker.is_alive())
        self._tmp.cleanup()
        self.assertFalse(self.root.exists())

    def test_sqlite_disk_io_during_record_is_not_unhandled_and_stop_still_joins(self) -> None:
        guard = _ThreadExceptionGuard()
        stop, worker = self._start_discovery()
        with guard, mock.patch.object(
            local_network,
            "save_payload",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            for _ in range(8):
                self._send({
                    "service": local_network.DISCOVERY_SERVICE,
                    "drone_id": "io-peer",
                    "name": "io-peer",
                    "scheme": "http",
                    "api_port": 8080,
                })
                time.sleep(0.05)
            self.assertTrue(worker.is_alive(), "sqlite3.Error must not kill the worker before stop")
            stop.set()
            worker.join(timeout=8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(guard.failures, [])
        self._tmp.cleanup()
        self.assertFalse(self.root.exists())

    def test_stop_leaves_discovery_dead_not_looping_on_swallowed_errors(self) -> None:
        stop, worker = self._start_discovery()
        stop.set()
        worker.join(timeout=8)
        self.assertFalse(worker.is_alive())
        self._tmp.cleanup()
        time.sleep(0.5)
        self._send({
            "service": local_network.DISCOVERY_SERVICE,
            "drone_id": "ghost",
            "name": "ghost",
            "scheme": "http",
            "api_port": 8080,
        })
        time.sleep(0.5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.db_path.exists())

    def test_health_loop_does_not_write_peer_checks_after_stop_and_db_removal(self) -> None:
        """Health is only signalled, not joined; after sleep it still must not touch a deleted DB."""
        guard = _ThreadExceptionGuard()
        cert = mock.Mock()
        cert.metadata.return_value = {"fingerprint": ""}
        opens_after_stop: list[str] = []
        stopped = threading.Event()
        real_open = state_store.open_database

        def spy_open(path, *args, **kwargs):
            if stopped.is_set():
                opens_after_stop.append(str(path))
            return real_open(path, *args, **kwargs)

        with guard, \
                mock.patch.object(state_store, "open_database", side_effect=spy_open), \
                mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
                mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"), \
                mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True}), \
                mock.patch.object(peer_workers, "_repair_tailnet_data_path"), \
                mock.patch.object(peer_workers, "DroneCertificateManager", return_value=cert):
            peer_workers._start_local_network_workers(self.settings)
            self.assertTrue(_health_threads())
            peer_workers.stop_local_network_workers(timeout=8.0)
            self.assertEqual(_discovery_threads(), [])
            stopped.set()
            self._tmp.cleanup()
            # First health pass sleeps DRONE_LOCAL_HEALTH_INTERVAL_SECONDS (min 10).
            time.sleep(12)

        self.assertEqual(opens_after_stop, [], opens_after_stop)
        self.assertEqual(
            [f"{error.__class__.__name__}: {error}" for error in guard.failures if isinstance(error, sqlite3.Error)],
            [],
        )
        self.assertFalse(self.root.exists())
