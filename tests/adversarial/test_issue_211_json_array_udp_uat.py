"""Issue #211 UAT: JSON-array UDP must not kill LAN discovery.

Derived from the issue before treating the delivery FakeSocket test as truth:

- ``json.loads(b'[]')`` succeeds and yields a list.
- ``record_discovered_peer`` immediately calls ``payload.get``.
- The original worker ``except`` listed OSError, ValueError, JSONDecodeError,
  and sqlite3.Error only, so AttributeError escaped the thread and the
  daemon exited.
- A later well-formed announcement must still be persisted. Thread death is
  the user-visible failure, not merely skipping one datagram.
- An array that *contains* a well-formed peer object is still not a peer
  announcement; that drone_id must not appear in the discovered map.
- Stop+join must still finish after a burst of array datagrams.

The delivery unit test mocks ``record_discovered_peer``, so AttributeError
never happens there and ``assertFalse(worker.is_alive())`` after join also
passes if the thread already crashed. These cases use a real UDP bind and
the real recorder.
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


class JsonArrayDiscoveryUAT(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-211-uat-")
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
                "DRONE_DEVICE_ID": "issue-211-host",
                "HTTPS_PORT": "8443",
                "HTTP_ONLY": "1",
                "ROM_METADATA_POLL_SECONDS": "0",
                "LOG_DIR": str(self.root / "logs"),
                "TLS_SELF_SIGNED_DIR": str(self.userdata / "system" / "certs"),
            },
            clear=False,
        )
        self._env.start()
        self.settings = Settings.from_env()
        self._ready = threading.Event()
        self._port_patch = mock.patch.object(local_network, "DISCOVERY_PORT", self.port)
        self._interval_patch = mock.patch.object(local_network, "DISCOVERY_INTERVAL_SECONDS", 3600)
        self._join_patch = mock.patch.object(
            local_network, "_join_multicast_group", side_effect=lambda sock: self._ready.set()
        )
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

    def _send(self, payload: dict | list | bytes) -> None:
        if isinstance(payload, bytes):
            packet = payload
        else:
            packet = json.dumps(payload).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(packet, ("127.0.0.1", self.port))

    def _peer_payload(self, drone_id: str) -> dict:
        return {
            "service": local_network.DISCOVERY_SERVICE,
            "kind": "announce",
            "drone_id": drone_id,
            "name": drone_id,
            "scheme": "http",
            "api_port": 8080,
        }

    def _start_discovery(self) -> tuple[threading.Event, threading.Thread, list[str]]:
        stop = threading.Event()
        seen: list[str] = []

        def on_discovery(peer: dict) -> None:
            seen.append(str(peer.get("drone_id") or ""))

        worker = local_network.start_discovery_worker(
            self.settings, lambda: "", on_discovery=on_discovery, stop_event=stop
        )
        self.assertTrue(self._ready.wait(5), "discovery worker did not bind")
        self.assertTrue(worker.is_alive(), "discovery worker exited before any datagram")
        return stop, worker, seen

    def _wait_for_peer(self, drone_id: str) -> dict:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            self._send(self._peer_payload(drone_id))
            peers = self._peers()
            if drone_id in peers:
                return peers
            time.sleep(0.1)
        self.fail(f"discovery worker did not persist {drone_id} after the malformed datagrams")

    def _peers(self) -> dict:
        try:
            peers = state_store.load_payload(self.db_path, "local_discovered_peers", {})
        except sqlite3.OperationalError:
            return {}
        return peers if isinstance(peers, dict) else {}

    def test_empty_json_array_udp_leaves_worker_alive_and_later_peer_is_recorded(self) -> None:
        """Reproduction from the issue: bind, send b'[]', worker must keep listening."""
        guard = _ThreadExceptionGuard()
        stop, worker, seen = self._start_discovery()
        with guard:
            self._send(b"[]")
            time.sleep(0.3)
            self.assertTrue(worker.is_alive(), "JSON-array UDP killed the discovery worker")
            self.assertEqual(guard.failures, [])
            peers = self._wait_for_peer("issue-211-after-array")
            self.assertIn("issue-211-after-array", peers)
            self.assertIn("issue-211-after-array", seen)
            stop.set()
            worker.join(timeout=8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(guard.failures, [])

    def test_array_wrapped_peer_object_is_not_recorded_then_dict_announcement_is(self) -> None:
        wrapped_id = "issue-211-wrapped"
        live_id = "issue-211-live"
        guard = _ThreadExceptionGuard()
        stop, worker, seen = self._start_discovery()
        with guard:
            for _ in range(8):
                self._send([self._peer_payload(wrapped_id)])
            time.sleep(0.4)
            self.assertTrue(worker.is_alive())
            peers = self._wait_for_peer(live_id)
            self.assertNotIn(wrapped_id, peers)
            self.assertNotIn(wrapped_id, seen)
            self.assertIn(live_id, peers)
            self.assertIn(live_id, seen)
            stop.set()
            worker.join(timeout=8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(
            [f"{error.__class__.__name__}: {error}" for error in guard.failures],
            [],
        )

    def test_burst_of_empty_arrays_still_allows_stop_join(self) -> None:
        guard = _ThreadExceptionGuard()
        stop, worker, _seen = self._start_discovery()
        with guard:
            for _ in range(40):
                self._send(b"[]")
            time.sleep(0.4)
            self.assertTrue(worker.is_alive(), "burst of [] datagrams killed discovery")
            stop.set()
            worker.join(timeout=8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(guard.failures, [])
        self.assertEqual(self._peers(), {})
        self._tmp.cleanup()
        self.assertFalse(self.root.exists())
