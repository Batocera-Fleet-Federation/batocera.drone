import tempfile
import threading
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


if __name__ == "__main__":
    unittest.main()
