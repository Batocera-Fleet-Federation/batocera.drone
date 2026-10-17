"""Adversarial coverage for issue #306's local peer-health retirement bound."""
from __future__ import annotations

import os
import threading
import time
import unittest
from unittest import mock

from app.transfer import local_network, peer_workers


class PeerHealthInterruptibleSleepTests(unittest.TestCase):
    def setUp(self) -> None:
        peer_workers.stop_local_network_workers(timeout=1.0)
        self._env = mock.patch.dict(os.environ, {
            "DRONE_LOCAL_HEALTH_INTERVAL_SECONDS": "3600",
            "DRONE_TAILNET_WATCHDOG_INTERVAL_SECONDS": "30",
            "DRONE_TAILNET_BOOTSTRAP_INTERVAL_SECONDS": "60",
        })
        self._env.start()
        self.addCleanup(self._env.stop)
        self._patchers = [
            mock.patch.object(peer_workers, "ensure_tailnet_networking"),
            mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"),
            mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True}),
            mock.patch.object(peer_workers, "_repair_tailnet_data_path"),
            mock.patch.object(local_network, "start_discovery_worker", return_value=mock.Mock()),
            mock.patch.object(local_network, "is_local_mode", return_value=False),
        ]
        self.mocks = [patcher.start() for patcher in self._patchers]
        for patcher in self._patchers:
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        peer_workers.stop_local_network_workers(timeout=1.0)

    def test_sleep_helper_slices_long_delay_and_observes_stop_between_slices(self) -> None:
        stop = threading.Event()
        requested: list[float] = []

        def fake_sleep(seconds: float) -> None:
            requested.append(seconds)
            stop.set()

        with mock.patch.object(peer_workers.time, "sleep", side_effect=fake_sleep):
            stopped = peer_workers._sleep_unless_stopped(stop, 3600, slice_seconds=1.0)

        self.assertTrue(stopped)
        self.assertEqual(requested, [1.0])

    def test_zero_or_negative_delay_returns_without_sleeping(self) -> None:
        stop = threading.Event()
        with mock.patch.object(peer_workers.time, "sleep") as sleep:
            self.assertFalse(peer_workers._sleep_unless_stopped(stop, 0))
            self.assertFalse(peer_workers._sleep_unless_stopped(stop, -2))
        sleep.assert_not_called()

    def test_stopping_long_interval_health_worker_retires_promptly(self) -> None:
        created: list[threading.Thread] = []
        real_thread = threading.Thread

        def record_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            created.append(thread)
            return thread

        with mock.patch.object(peer_workers, "Thread", side_effect=record_thread):
            peer_workers._start_local_network_workers(mock.Mock())
            health = next(thread for thread in created if thread.name == "drone-local-peer-health")
            self.assertTrue(health.is_alive())
            started = time.monotonic()
            peer_workers.stop_local_network_workers(timeout=1.0)
            health.join(timeout=2.0)

        self.assertFalse(health.is_alive(), "health worker remained asleep after generation stop")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_restart_retires_previous_health_generation(self) -> None:
        created: list[threading.Thread] = []
        real_thread = threading.Thread

        def record_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            created.append(thread)
            return thread

        with mock.patch.object(peer_workers, "Thread", side_effect=record_thread):
            peer_workers._start_local_network_workers(mock.Mock())
            first = next(t for t in created if t.name == "drone-local-peer-health")
            peer_workers._start_local_network_workers(mock.Mock())
            health_threads = [t for t in created if t.name == "drone-local-peer-health"]
            self.assertEqual(len(health_threads), 2)
            health_threads[1].join(timeout=0.1)
            first.join(timeout=2.0)

        self.assertFalse(first.is_alive(), "replaced health generation was not retired")
        self.assertTrue(health_threads[1].is_alive(), "new health generation should remain active")
