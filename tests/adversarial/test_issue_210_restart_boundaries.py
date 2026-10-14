"""Issue #210 boundaries: restart ordering, in-flight join, and error paths.

Invariants taken from the issue before treating the patch as ground truth:

- A live generation's stop event must be signalled before its handles are
  replaced, otherwise ``stop_local_network_workers`` cannot reach it.
- The first discovery thread must be joined before a second
  ``start_discovery_worker`` runs; signalling without join still orphans a
  blocked listener.
- Restart after a failed second start must not revive the first generation.
- ``stop_local_network_workers`` remains a no-op / idempotent when idle
  (pinned by #191; re-checked so restart-via-stop cannot break that).
- A malformed ``timeout`` on an explicit stop must fail without starting a
  replacement generation.
- An in-flight ``_save_peer_map`` on the first generation must delay the
  second start until that join can finish; otherwise the first daemon stays
  alive and untracked.

Tailnet watchdog / bootstrap threads ignore the stop event entirely; that is
outside this issue's discovery-thread scope and is not asserted here.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_workers


def _free_udp_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


def _discovery_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "drone-local-discovery"]


class RestartBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-210-bound-")
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
        self._port_patch.start()
        self._interval_patch.start()
        self._join_patch.start()
        self._announce_patch.start()
        self._cert_patch.start()
        self.addCleanup(self._tmp.cleanup)
        peer_workers.stop_local_network_workers(timeout=3.0)

    def tearDown(self) -> None:
        peer_workers.stop_local_network_workers(timeout=3.0)
        for thread in _discovery_threads():
            thread.join(timeout=3.0)
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

    def _send(self, payload: dict) -> None:
        packet = json.dumps(payload).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(packet, ("127.0.0.1", self.port))

    def test_second_start_signals_first_stop_event_and_uses_a_new_one(self) -> None:
        first = mock.Mock()
        second = mock.Mock()
        with mock.patch.object(peer_workers, "Thread", side_effect=self._stub_thread), \
                mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
                mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"), \
                mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True}), \
                mock.patch.object(peer_workers, "_repair_tailnet_data_path"), \
                mock.patch.object(
                    local_network, "start_discovery_worker", side_effect=[first, second]
                ) as start:
            peer_workers._start_local_network_workers(self.settings)
            first_stop = start.call_args.kwargs["stop_event"]
            self.assertFalse(first_stop.is_set())
            peer_workers._start_local_network_workers(self.settings)
            second_stop = start.call_args.kwargs["stop_event"]
        self.assertEqual(start.call_count, 2)
        self.assertTrue(first_stop.is_set())
        self.assertIsNot(second_stop, first_stop)
        self.assertFalse(second_stop.is_set())
        first.join.assert_called_once()
        self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, second)
        self.assertIs(peer_workers._LOCAL_NETWORK_STOP, second_stop)

    def test_first_discovery_is_joined_before_second_start_discovery_worker(self) -> None:
        order: list[str] = []
        first = mock.Mock()

        def join_first(*args, **kwargs) -> None:
            order.append("join-first")

        first.join.side_effect = join_first
        second = mock.Mock()

        def start_worker(*args, **kwargs):
            order.append("start-discovery")
            return first if start_worker.calls == 0 else second

        start_worker.calls = 0

        def counting_start(*args, **kwargs):
            result = start_worker(*args, **kwargs)
            start_worker.calls += 1
            return result

        with mock.patch.object(peer_workers, "Thread", side_effect=self._stub_thread), \
                mock.patch.object(
                    local_network, "start_discovery_worker", side_effect=counting_start
                ):
            peer_workers._start_local_network_workers(self.settings)
            peer_workers._start_local_network_workers(self.settings)
        self.assertEqual(order, ["start-discovery", "join-first", "start-discovery"])

    def test_failed_second_start_does_not_leave_first_discovery_alive(self) -> None:
        first = mock.Mock()
        with mock.patch.object(peer_workers, "Thread", side_effect=self._stub_thread), \
                mock.patch.object(
                    local_network,
                    "start_discovery_worker",
                    side_effect=[first, RuntimeError("second generation bind failed")],
                ):
            peer_workers._start_local_network_workers(self.settings)
            with self.assertRaisesRegex(RuntimeError, "second generation bind failed"):
                peer_workers._start_local_network_workers(self.settings)
        first.join.assert_called_once()
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)

    def test_malformed_stop_timeout_does_not_replace_the_live_generation(self) -> None:
        with mock.patch.object(peer_workers, "Thread", side_effect=self._stub_thread):
            peer_workers._start_local_network_workers(self.settings)
            live = peer_workers._LOCAL_NETWORK_DISCOVERY
            live_stop = peer_workers._LOCAL_NETWORK_STOP
            self.assertIsNotNone(live)
            self.assertIsNotNone(live_stop)
            with self.assertRaises(TypeError):
                peer_workers.stop_local_network_workers(timeout="not-a-timeout")
            self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, live)
            self.assertIs(peer_workers._LOCAL_NETWORK_STOP, live_stop)
            self.assertTrue(live_stop.is_set())
            peer_workers.stop_local_network_workers(timeout=1.0)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)
        self.assertFalse(live.is_alive())

    def test_idle_stop_is_still_a_noop_before_restart(self) -> None:
        peer_workers._LOCAL_NETWORK_STOP = None
        peer_workers._LOCAL_NETWORK_DISCOVERY = None
        peer_workers.stop_local_network_workers()
        peer_workers.stop_local_network_workers(timeout=0.1)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)

    def test_second_start_waits_out_in_flight_first_generation_save(self) -> None:
        started = threading.Event()
        release = threading.Event()
        real_save = local_network.save_payload

        def blocking_save(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(timeout=8), "test failed to release in-flight save")
            return real_save(*args, **kwargs)

        restart_done = threading.Event()
        restart_error: list[BaseException] = []

        def restart() -> None:
            try:
                peer_workers._start_local_network_workers(self.settings)
            except BaseException as error:  # pragma: no cover - surfaced below
                restart_error.append(error)
            finally:
                restart_done.set()

        with mock.patch.object(peer_workers, "Thread", side_effect=self._stub_thread), \
                mock.patch.object(local_network, "save_payload", side_effect=blocking_save):
            peer_workers._start_local_network_workers(self.settings)
            first = peer_workers._LOCAL_NETWORK_DISCOVERY
            self.assertIsNotNone(first)
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and not started.is_set():
                self._send(
                    {
                        "service": local_network.DISCOVERY_SERVICE,
                        "kind": "announce",
                        "drone_id": "issue-210-blocked",
                        "name": "issue-210-blocked",
                        "scheme": "http",
                        "api_port": 8080,
                    }
                )
                time.sleep(0.05)
            self.assertTrue(started.is_set(), "first generation never entered _save_peer_map")

            joiner = threading.Thread(target=restart, name="issue-210-restart")
            joiner.start()
            time.sleep(0.4)
            self.assertFalse(restart_done.is_set(), "second start returned while first save was blocked")
            self.assertTrue(first.is_alive())
            self.assertEqual(_discovery_threads(), [first])

            release.set()
            joiner.join(timeout=8)
            self.assertTrue(restart_done.is_set())
            self.assertEqual(restart_error, [])
            self.assertFalse(first.is_alive())
            live = _discovery_threads()
            self.assertEqual(len(live), 1)
            self.assertIsNot(live[0], first)
            self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, live[0])

        peer_workers.stop_local_network_workers(timeout=8.0)
        self.assertEqual(_discovery_threads(), [])


if __name__ == "__main__":
    unittest.main()
