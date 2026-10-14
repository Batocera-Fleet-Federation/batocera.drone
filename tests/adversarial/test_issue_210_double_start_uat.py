"""Issue #210 UAT: a second _start_local_network_workers must not orphan discovery.

The reported failure is a real leftover ``drone-local-discovery`` daemon: the
second start overwrote ``_LOCAL_NETWORK_STOP`` / ``_LOCAL_NETWORK_DISCOVERY``,
so a later ``stop_local_network_workers`` joined only the latest Thread.

Expected behavior, from the issue (not from the delivery unit test):

- After a second (or third) start, exactly one discovery thread is alive.
- The previous generation's thread is dead, not merely untracked.
- ``stop_local_network_workers`` after a restart joins that live generation
  and leaves zero discovery threads.
- Start -> stop -> start is a clean new generation, not a pile of daemons.
- The live generation still records injected peers; a dead first generation
  does not keep writing after restart.

``create_server`` gating is issue #191's single-start path. These cases call
the helper twice, which is the reproduction.
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


class DoubleStartDiscoveryUAT(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-210-uat-")
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
                "DRONE_DEVICE_ID": "issue-210-host",
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
        cert = mock.Mock()
        cert.metadata.return_value = {"fingerprint": ""}
        self._cert_patch = mock.patch.object(peer_workers, "DroneCertificateManager", return_value=cert)
        self._thread_patch = mock.patch.object(peer_workers, "Thread", side_effect=self._stub_thread)
        self._port_patch.start()
        self._interval_patch.start()
        self._join_patch.start()
        self._announce_patch.start()
        self._cert_patch.start()
        self._thread_patch.start()
        self.addCleanup(self._tmp.cleanup)
        peer_workers.stop_local_network_workers(timeout=3.0)

    def tearDown(self) -> None:
        self._reset_workers()
        self._thread_patch.stop()
        self._cert_patch.stop()
        self._announce_patch.stop()
        self._join_patch.stop()
        self._interval_patch.stop()
        self._port_patch.stop()
        self._env.stop()

    def _stub_thread(self, *args, **kwargs):
        thread = mock.Mock()
        thread.name = kwargs.get("name", "")
        thread.start = lambda: None
        thread.is_alive.return_value = False
        return thread

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

    def _wait_for_peer(self, drone_id: str) -> None:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            self._send(self._peer_payload(drone_id))
            try:
                peers = state_store.load_payload(self.db_path, "local_discovered_peers", {})
            except sqlite3.OperationalError:
                peers = {}
            if isinstance(peers, dict) and drone_id in peers:
                return
            time.sleep(0.1)
        self.fail(f"live discovery generation did not persist {drone_id}")

    def _start(self) -> None:
        peer_workers._start_local_network_workers(self.settings)

    def test_second_start_leaves_one_live_discovery_and_stop_joins_it(self) -> None:
        """Reproduction: two starts, then stop; no leftover drone-local-discovery."""
        guard = _ThreadExceptionGuard()
        with guard:
            self._start()
            first_gen = _discovery_threads()
            self.assertEqual(len(first_gen), 1)
            first = first_gen[0]
            self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, first)
            self._wait_for_peer("issue-210-first")

            self._start()
            live = _discovery_threads()
            self.assertEqual(len(live), 1, [thread.ident for thread in live])
            second = live[0]
            self.assertFalse(first.is_alive())
            self.assertIsNot(second, first)
            self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, second)
            self._wait_for_peer("issue-210-second")

            peer_workers.stop_local_network_workers(timeout=8.0)
            self.assertEqual(_discovery_threads(), [])
            self.assertFalse(second.is_alive())
            self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)
            self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)

        self.assertEqual(guard.failures, [])

    def test_triple_start_still_has_a_single_live_discovery_thread(self) -> None:
        seen: list[threading.Thread] = []
        for _ in range(3):
            self._start()
            live = _discovery_threads()
            self.assertEqual(len(live), 1)
            current = live[0]
            for previous in seen:
                self.assertFalse(previous.is_alive())
                self.assertIsNot(current, previous)
            seen.append(current)
        self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, seen[-1])
        peer_workers.stop_local_network_workers(timeout=8.0)
        self.assertEqual(_discovery_threads(), [])
        self.assertFalse(seen[-1].is_alive())

    def test_start_stop_start_does_not_accumulate_discovery_daemons(self) -> None:
        self._start()
        first = peer_workers._LOCAL_NETWORK_DISCOVERY
        self.assertIsNotNone(first)
        peer_workers.stop_local_network_workers(timeout=8.0)
        self.assertFalse(first.is_alive())
        self.assertEqual(_discovery_threads(), [])

        self._start()
        second = peer_workers._LOCAL_NETWORK_DISCOVERY
        self.assertIsNotNone(second)
        self.assertIsNot(second, first)
        self.assertEqual(_discovery_threads(), [second])
        self._wait_for_peer("issue-210-restarted")
        peer_workers.stop_local_network_workers(timeout=8.0)
        self.assertEqual(_discovery_threads(), [])

    def test_dead_first_generation_does_not_keep_writing_after_restart(self) -> None:
        self._start()
        first = peer_workers._LOCAL_NETWORK_DISCOVERY
        self._wait_for_peer("issue-210-before-restart")
        self._start()
        self.assertFalse(first.is_alive())
        self._wait_for_peer("issue-210-after-restart")

        peers = state_store.load_payload(self.db_path, "local_discovered_peers", {})
        self.assertIn("issue-210-before-restart", peers)
        self.assertIn("issue-210-after-restart", peers)

        peer_workers.stop_local_network_workers(timeout=8.0)
        self.assertEqual(_discovery_threads(), [])
        late_id = "issue-210-after-stop"
        self._send(self._peer_payload(late_id))
        time.sleep(0.8)
        peers = state_store.load_payload(self.db_path, "local_discovered_peers", {})
        self.assertNotIn(late_id, peers)


if __name__ == "__main__":
    unittest.main()
