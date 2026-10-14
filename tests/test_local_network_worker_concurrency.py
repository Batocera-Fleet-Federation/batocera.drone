import threading
import time
import unittest
from unittest import mock

from app.transfer import local_network, peer_workers


class LocalNetworkWorkerConcurrencyTests(unittest.TestCase):
    def test_overlapping_starts_leave_no_orphaned_discovery_generation(self) -> None:
        # Each generation records its stop event and discovery mock so the test can
        # prove that every one of them was retired, not only the last tracked one.
        generations: list = []
        generations_lock = threading.Lock()

        def fake_discovery(settings, fingerprint, stop_event):
            # Widen the window between reading the old generation and publishing the new one.
            time.sleep(0.005)
            worker = mock.Mock()
            with generations_lock:
                generations.append((stop_event, worker))
            return worker

        barrier = threading.Barrier(8)
        errors: list = []

        def start_once() -> None:
            try:
                barrier.wait(timeout=5)
                peer_workers._start_local_network_workers(mock.Mock())
            except Exception as error:  # pragma: no cover - surfaced by the assertion below
                errors.append(error)

        with mock.patch.object(peer_workers, "Thread"), \
                mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
                mock.patch.object(local_network, "start_discovery_worker", side_effect=fake_discovery):
            threads = [threading.Thread(target=start_once) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(errors, [])
            live_stop, live_worker = generations[-1][0], generations[-1][1]
            self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, live_worker)
            peer_workers.stop_local_network_workers(timeout=1.0)

        self.assertGreaterEqual(len(generations), 1)
        for stop_event, worker in generations:
            self.assertTrue(stop_event.is_set())
            worker.join.assert_called_once()
        self.assertTrue(live_stop.is_set())
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)


if __name__ == "__main__":
    unittest.main()
