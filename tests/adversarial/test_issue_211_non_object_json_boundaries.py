"""Issue #211 boundaries: every JSON value that is not an object.

``json.loads`` of a LAN datagram can yield dict, list, str, int, float,
bool, or None. Only a dict is a discovery announcement. The issue names
``b'[]'`` because that path raises AttributeError on ``payload.get``;
null/true/false/numbers/strings/NaN/Infinity take the same uncaught path.

Expected behavior from the issue (not from the FakeSocket unit test):

- Non-object JSON does not terminate the worker.
- threading.excepthook sees no AttributeError (or any other unhandled error).
- Those datagrams are not persisted as discovered peers.
- An empty JSON object ``{}`` is a dict, so it may reach the recorder, but
  it is not a valid announcement (wrong service / no drone_id) and must
  not create a row.
- After the junk sequence, a well-formed dict announcement is recorded.
- Decode errors (invalid UTF-8, trailing garbage) already mapped to
  ValueError/JSONDecodeError must keep the same "stay alive" contract.
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


# Python's json.loads accepts NaN/Infinity; each is a float, not a dict.
_NON_OBJECT_JSON = (
    b"[]",
    b"null",
    b"true",
    b"false",
    b"0",
    b"42",
    b"-1",
    b"1.5",
    b'"peer"',
    b"[1, 2]",
    b"[{}]",
    b" \t\n[]\n",
    b"NaN",
    b"Infinity",
    b"-Infinity",
    json.dumps([[{"service": "batocera-drone-local-v1", "drone_id": "nested"}]]).encode("utf-8"),
)

_RELATED_MALFORMED = (
    b"",
    b"\xff\xfe not-utf8",
    b"[]\x00",
    b"{",
    b"]",
    b"undefined",
)


class NonObjectJsonDiscoveryBoundaries(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-211-bound-")
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

    def _send(self, payload: dict | bytes) -> None:
        packet = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
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

    def _start_discovery(self) -> tuple[threading.Event, threading.Thread]:
        stop = threading.Event()
        worker = local_network.start_discovery_worker(self.settings, lambda: "", stop_event=stop)
        self.assertTrue(self._ready.wait(5), "discovery worker did not bind")
        self.assertTrue(worker.is_alive())
        return stop, worker

    def _peers(self) -> dict:
        try:
            peers = state_store.load_payload(self.db_path, "local_discovered_peers", {})
        except sqlite3.OperationalError:
            return {}
        return peers if isinstance(peers, dict) else {}

    def _wait_for_peer(self, drone_id: str) -> dict:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            self._send(self._peer_payload(drone_id))
            peers = self._peers()
            if drone_id in peers:
                return peers
            time.sleep(0.1)
        self.fail(f"discovery worker did not persist {drone_id} after non-object JSON")

    def test_each_non_object_json_type_leaves_worker_alive(self) -> None:
        guard = _ThreadExceptionGuard()
        stop, worker = self._start_discovery()
        with guard:
            for packet in _NON_OBJECT_JSON:
                self._send(packet)
                self.assertTrue(
                    worker.is_alive(),
                    f"worker died after {packet!r}: {guard.failures!r}",
                )
            time.sleep(0.4)
            self.assertTrue(worker.is_alive())
            self.assertEqual(self._peers(), {})
            self.assertEqual(guard.failures, [])
            stop.set()
            worker.join(timeout=8)
        self.assertFalse(worker.is_alive())

    def test_empty_object_is_not_a_peer_and_does_not_kill_worker(self) -> None:
        guard = _ThreadExceptionGuard()
        stop, worker = self._start_discovery()
        with guard:
            self._send(b"{}")
            time.sleep(0.3)
            self.assertTrue(worker.is_alive())
            self.assertEqual(self._peers(), {})
            peers = self._wait_for_peer("issue-211-after-empty-object")
            self.assertEqual(set(peers), {"issue-211-after-empty-object"})
            stop.set()
            worker.join(timeout=8)
        self.assertEqual(guard.failures, [])

    def test_non_object_json_then_valid_announcement_is_recorded(self) -> None:
        guard = _ThreadExceptionGuard()
        stop, worker = self._start_discovery()
        with guard:
            for packet in _NON_OBJECT_JSON + _RELATED_MALFORMED:
                self._send(packet)
            time.sleep(0.4)
            self.assertTrue(worker.is_alive(), f"worker died on junk: {guard.failures!r}")
            peers = self._wait_for_peer("issue-211-survivor")
            self.assertIn("issue-211-survivor", peers)
            self.assertNotIn("peer", peers)
            self.assertNotIn("nested", peers)
            stop.set()
            worker.join(timeout=8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(
            [f"{error.__class__.__name__}: {error}" for error in guard.failures],
            [],
        )

    def test_decode_errors_are_not_unhandled_thread_exceptions(self) -> None:
        guard = _ThreadExceptionGuard()
        stop, worker = self._start_discovery()
        with guard:
            for packet in _RELATED_MALFORMED:
                self._send(packet)
            time.sleep(0.3)
            self.assertTrue(worker.is_alive())
            stop.set()
            worker.join(timeout=8)
        self.assertEqual(guard.failures, [])
        self.assertFalse(worker.is_alive())
