"""Issue #237 UAT: a concurrent stop cannot lose a half-started discovery worker.

The issue reports a TOCTOU between callers of ``_start_local_network_workers``:
one caller could publish a discovery worker after another caller had already
stopped and cleared the module globals.  The observable contract is that a
generation which overlaps a completed stop is not left live and untracked.

This interleaving blocks discovery construction after the new stop event has
been created.  It deliberately avoids network I/O and background tailnet loops
so the lifecycle ordering, rather than timing or host networking, is tested.
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from app.transfer import local_network, peer_workers


class _DiscoveryWorker:
    def __init__(self) -> None:
        self.close_calls = 0
        self.join_timeouts: list[float | None] = []

    def close_discovery_socket(self) -> None:
        self.close_calls += 1

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)


class ConcurrentLocalNetworkLifecycleUAT(unittest.TestCase):
    def setUp(self) -> None:
        peer_workers.stop_local_network_workers(timeout=1.0)
        self.addCleanup(peer_workers.stop_local_network_workers, 1.0)

    def test_stop_racing_a_blocked_start_retires_the_published_discovery(self) -> None:
        """A stop during discovery construction cannot leave a post-stop handle."""
        discovery_entered = threading.Event()
        allow_discovery_to_finish = threading.Event()
        stop_entered = threading.Event()
        stop_finished = threading.Event()
        worker = _DiscoveryWorker()
        errors: list[BaseException] = []

        def start_discovery(*args: object, **kwargs: object) -> _DiscoveryWorker:
            discovery_entered.set()
            self.assertTrue(
                allow_discovery_to_finish.wait(5),
                "test did not release the blocked discovery construction",
            )
            return worker

        def inert_thread(*args: object, **kwargs: object) -> mock.Mock:
            thread = mock.Mock()
            thread.start.return_value = None
            return thread

        def begin_start() -> None:
            try:
                peer_workers._start_local_network_workers(mock.Mock())
            except BaseException as error:  # surfaced by the main test thread
                errors.append(error)

        def begin_stop() -> None:
            stop_entered.set()
            try:
                peer_workers.stop_local_network_workers(timeout=1.0)
            except BaseException as error:  # surfaced by the main test thread
                errors.append(error)
            finally:
                stop_finished.set()

        certificate = mock.Mock()
        certificate.metadata.return_value = {"fingerprint": ""}
        with mock.patch.object(peer_workers, "Thread", side_effect=inert_thread), \
                mock.patch.object(peer_workers, "DroneCertificateManager", return_value=certificate), \
                mock.patch.object(local_network, "start_discovery_worker", side_effect=start_discovery):
            starting = threading.Thread(target=begin_start)
            starting.start()
            self.assertTrue(discovery_entered.wait(3), "start never reached discovery construction")

            stopping = threading.Thread(target=begin_stop)
            stopping.start()
            self.assertTrue(stop_entered.wait(3), "stop thread did not begin")

            # A serialized implementation holds stop until start has published its
            # worker.  An implementation that lets stop return first must still
            # arrange for this subsequently published worker to be retired.
            stop_finished.wait(0.5)
            allow_discovery_to_finish.set()
            starting.join(timeout=3)
            stopping.join(timeout=3)

        self.assertFalse(starting.is_alive(), "start call did not complete")
        self.assertFalse(stopping.is_alive(), "stop call did not complete")
        self.assertEqual(errors, [])
        self.assertEqual(worker.close_calls, 1)
        self.assertEqual(worker.join_timeouts, [1.0])
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)

    def test_overlapping_starts_retire_every_superseded_discovery_generation(self) -> None:
        """The reported two-start race cannot hide the first published worker."""
        first_discovery_entered = threading.Event()
        second_discovery_entered = threading.Event()
        allow_first_discovery_to_finish = threading.Event()
        workers = [_DiscoveryWorker(), _DiscoveryWorker()]
        calls = 0
        calls_lock = threading.Lock()
        errors: list[BaseException] = []

        def start_discovery(*args: object, **kwargs: object) -> _DiscoveryWorker:
            nonlocal calls
            with calls_lock:
                call = calls
                calls += 1
            if call == 0:
                first_discovery_entered.set()
                self.assertTrue(allow_first_discovery_to_finish.wait(5))
            elif call == 1:
                second_discovery_entered.set()
            else:  # pragma: no cover - two callers are the test fixture
                self.fail(f"unexpected discovery generation {call + 1}")
            return workers[call]

        def inert_thread(*args: object, **kwargs: object) -> mock.Mock:
            thread = mock.Mock()
            thread.start.return_value = None
            return thread

        def begin_start() -> None:
            try:
                peer_workers._start_local_network_workers(mock.Mock())
            except BaseException as error:  # surfaced by the main test thread
                errors.append(error)

        certificate = mock.Mock()
        certificate.metadata.return_value = {"fingerprint": ""}
        with mock.patch.object(peer_workers, "Thread", side_effect=inert_thread), \
                mock.patch.object(peer_workers, "DroneCertificateManager", return_value=certificate), \
                mock.patch.object(local_network, "start_discovery_worker", side_effect=start_discovery):
            first_start = threading.Thread(target=begin_start)
            first_start.start()
            self.assertTrue(first_discovery_entered.wait(3), "first start did not reach discovery")

            second_start = threading.Thread(target=begin_start)
            second_start.start()
            # Without lifecycle serialization, the second start reaches this
            # factory and publishes its handle before the first one is released.
            second_discovery_entered.wait(0.5)
            allow_first_discovery_to_finish.set()
            first_start.join(timeout=3)
            second_start.join(timeout=3)
            peer_workers.stop_local_network_workers(timeout=1.0)

        self.assertFalse(first_start.is_alive())
        self.assertFalse(second_start.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(calls, 2)
        for worker in workers:
            self.assertEqual(worker.close_calls, 1)
            self.assertEqual(len(worker.join_timeouts), 1)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)


if __name__ == "__main__":
    unittest.main()
