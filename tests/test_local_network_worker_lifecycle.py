import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app.transfer import local_network, peer_workers


class LocalNetworkWorkerLifecycleTests(unittest.TestCase):
    def test_discovery_worker_exits_when_stop_event_is_set(self) -> None:
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(local_network, "is_local_mode", return_value=True), \
                mock.patch.object(local_network, "_join_multicast_group"), \
                mock.patch.object(local_network, "announce"), \
                mock.patch.object(local_network, "record_discovered_peer", return_value=None):
            settings = mock.Mock(state_database_file=Path(tmp) / "state.sqlite3")
            worker = local_network.start_discovery_worker(settings, lambda: "", stop_event=stop)
            stop.set()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())

    def test_discovery_worker_survives_non_object_json_datagrams(self) -> None:
        stop = threading.Event()
        packets = [b"[]", b"null", b'"peer"', b"42", b"[1, 2]", b"{}"]
        recorded: list = []

        class FakeSocket:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def setsockopt(self, *args) -> None:
                pass

            def bind(self, *args) -> None:
                pass

            def settimeout(self, *args) -> None:
                pass

            def close(self) -> None:
                pass

            def recvfrom(self, size: int):
                if packets:
                    return packets.pop(0), ("192.0.2.10", 5000)
                stop.set()
                raise socket.timeout()

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(local_network, "is_local_mode", return_value=True), \
                mock.patch.object(local_network, "_join_multicast_group"), \
                mock.patch.object(local_network, "announce"), \
                mock.patch.object(local_network.socket, "socket", FakeSocket), \
                mock.patch.object(local_network, "record_discovered_peer",
                                  side_effect=lambda *a, **k: recorded.append(a[1]) or None):
            settings = mock.Mock(state_database_file=Path(tmp) / "state.sqlite3")
            worker = local_network.start_discovery_worker(settings, lambda: "", stop_event=stop)
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(recorded, [{}])

    def test_stop_local_network_workers_signals_and_joins_discovery(self) -> None:
        discovery = mock.Mock()
        with mock.patch.object(peer_workers, "Thread"), \
                mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
                mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"), \
                mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True}), \
                mock.patch.object(peer_workers, "_repair_tailnet_data_path"), \
                mock.patch.object(local_network, "start_discovery_worker", return_value=discovery) as start:
            peer_workers._start_local_network_workers(mock.Mock())
            stop_event = start.call_args.kwargs["stop_event"]
            self.assertFalse(stop_event.is_set())
            peer_workers.stop_local_network_workers(timeout=1.0)
        self.assertTrue(stop_event.is_set())
        discovery.join.assert_called_once_with(1.0)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)

    def test_running_flag_clears_after_direct_stop(self) -> None:
        # create_server latches its start once per process; the live-generation
        # check is what lets a later create_server restart after a teardown stop.
        with mock.patch.object(peer_workers, "Thread"), \
                mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
                mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True}), \
                mock.patch.object(local_network, "start_discovery_worker", return_value=mock.Mock()):
            self.assertFalse(peer_workers.local_network_workers_running())
            peer_workers._start_local_network_workers(mock.Mock())
            self.assertTrue(peer_workers.local_network_workers_running())
            peer_workers.stop_local_network_workers(timeout=1.0)
        self.assertFalse(peer_workers.local_network_workers_running())

    def test_second_start_does_not_orphan_first_discovery_thread(self) -> None:
        first = mock.Mock()
        second = mock.Mock()
        with mock.patch.object(peer_workers, "Thread"), \
                mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
                mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"), \
                mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True}), \
                mock.patch.object(peer_workers, "_repair_tailnet_data_path"), \
                mock.patch.object(local_network, "start_discovery_worker", side_effect=[first, second]) as start:
            peer_workers._start_local_network_workers(mock.Mock())
            first_stop = start.call_args.kwargs["stop_event"]
            peer_workers._start_local_network_workers(mock.Mock())
            self.assertEqual(start.call_count, 2)
            self.assertTrue(first_stop.is_set())
            first.join.assert_called_once()
            second_stop = start.call_args.kwargs["stop_event"]
            self.assertFalse(second_stop.is_set())
            peer_workers.stop_local_network_workers(timeout=1.0)
        self.assertTrue(second_stop.is_set())
        second.join.assert_called_once_with(1.0)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)

    def test_local_peer_health_loop_exits_promptly_on_stop(self) -> None:
        # A long health interval must not keep the thread asleep after its generation stops.
        created: list = []
        real_thread = threading.Thread

        def record_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            created.append(thread)
            return thread

        with mock.patch.dict("os.environ", {"DRONE_LOCAL_HEALTH_INTERVAL_SECONDS": "3600"}), \
                mock.patch.object(peer_workers, "Thread", side_effect=record_thread), \
                mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
                mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"), \
                mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True}), \
                mock.patch.object(peer_workers, "_repair_tailnet_data_path"), \
                mock.patch.object(local_network, "is_local_mode", return_value=False), \
                mock.patch.object(local_network, "start_discovery_worker", return_value=mock.Mock()):
            peer_workers._start_local_network_workers(mock.Mock())
            health = [t for t in created if t.name == "drone-local-peer-health"]
            self.assertEqual(len(health), 1)
            self.assertTrue(health[0].is_alive())
            peer_workers.stop_local_network_workers(timeout=1.0)
            health[0].join(timeout=5)
        self.assertFalse(health[0].is_alive())

    def test_restart_does_not_stack_or_leak_tailnet_loops(self) -> None:
        # Other tests can leave same-named daemon threads behind, so only count this test's threads.
        existing = set(threading.enumerate())

        def alive(name: str) -> int:
            return sum(1 for t in threading.enumerate() if t.name == name and t.is_alive() and t not in existing)

        def wait_for_count(name: str, expected: int) -> None:
            deadline = time.monotonic() + 5
            while alive(name) != expected and time.monotonic() < deadline:
                time.sleep(0.02)

        names = ("drone-tailnet-watchdog", "drone-tailnet-bootstrap-retry")
        discovery = mock.Mock()
        real_sleep = time.sleep
        # Real Thread targets; sleeps are shortened so the loops wake and must notice their stop.
        with mock.patch.object(peer_workers.time, "sleep", side_effect=lambda s: real_sleep(0.01)), \
                mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
                mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"), \
                mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True}), \
                mock.patch.object(peer_workers, "_repair_tailnet_data_path"), \
                mock.patch.object(local_network, "start_discovery_worker", return_value=discovery):
            peer_workers._start_local_network_workers(mock.Mock())
            for name in names:
                self.assertEqual(alive(name), 1)
            peer_workers._start_local_network_workers(mock.Mock())
            for name in names:
                wait_for_count(name, 1)
                self.assertEqual(alive(name), 1, name)
            peer_workers.stop_local_network_workers(timeout=1.0)
            for name in names:
                wait_for_count(name, 0)
                self.assertEqual(alive(name), 0, name)


if __name__ == "__main__":
    unittest.main()
