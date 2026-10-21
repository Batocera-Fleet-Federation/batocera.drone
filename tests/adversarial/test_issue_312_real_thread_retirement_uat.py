"""Issue #312 real-thread retirement of the local-network generation.

Expected behavior, derived from the issue before reading the delivery: when
``stop_local_network_workers`` returns, the discovery thread and the tailnet
watchdog / bootstrap-retry threads of the retired generation must be gone.
Their poll intervals are set to an hour, so a stop that merely flips a flag
and does not wake them would leave them alive. Nothing here fakes a thread;
only network I/O (multicast announce, tailnet calls) is patched.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from app.transfer import local_network, peer_workers


GENERATION_THREADS = (
    "drone-local-discovery",
    "drone-tailnet-watchdog",
    "drone-tailnet-bootstrap-retry",
    "drone-tailnet-peer-autopair",
)


def _generation_threads(baseline: set[int]) -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.ident not in baseline and thread.name in GENERATION_THREADS and thread.is_alive()
    ]


class RealThreadRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.baseline = {thread.ident for thread in threading.enumerate()}
        patches = [
            mock.patch.dict(
                "os.environ",
                {
                    "DRONE_TAILNET_WATCHDOG_INTERVAL_SECONDS": "3600",
                    "DRONE_TAILNET_BOOTSTRAP_INTERVAL_SECONDS": "3600",
                    "DRONE_TAILNET_PEER_SYNC_INTERVAL_SECONDS": "3600",
                    "DRONE_LOCAL_HEALTH_INTERVAL_SECONDS": "3600",
                },
            ),
            mock.patch.object(peer_workers, "ensure_tailnet_networking"),
            mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"),
            mock.patch.object(peer_workers, "tailnet_status", return_value={"installed": False, "running": False}),
            mock.patch.object(peer_workers, "_repair_tailnet_data_path"),
            mock.patch.object(peer_workers, "DroneCertificateManager"),
            mock.patch.object(local_network, "is_local_mode", return_value=True),
            mock.patch.object(local_network, "announce"),
            mock.patch.object(local_network, "DISCOVERY_PORT", 0),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(peer_workers.stop_local_network_workers)

    def _start(self) -> None:
        peer_workers._start_local_network_workers(mock.Mock())
        self.assertTrue(peer_workers.local_network_workers_running())

    def _wait_for_no_generation_threads(self, timeout: float) -> list[threading.Thread]:
        deadline = time.monotonic() + timeout
        leftover = _generation_threads(self.baseline)
        while leftover and time.monotonic() < deadline:
            time.sleep(0.02)
            leftover = _generation_threads(self.baseline)
        return leftover

    def test_stop_wakes_sleeping_discovery_and_tailnet_threads(self) -> None:
        self._start()
        # Sanity: the long-interval threads are really parked, so exiting them
        # below can only come from the generation stop.
        live = {thread.name for thread in _generation_threads(self.baseline)}
        self.assertIn("drone-tailnet-watchdog", live)
        self.assertIn("drone-tailnet-bootstrap-retry", live)

        started = time.monotonic()
        peer_workers.stop_local_network_workers(timeout=2.0)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0, "stop must not wait out the hour-long poll intervals")
        leftover = self._wait_for_no_generation_threads(timeout=2.0)
        self.assertEqual([], [thread.name for thread in leftover])

    def test_repeated_start_stop_cycles_leave_no_generation_threads(self) -> None:
        for _ in range(3):
            self._start()
            peer_workers.stop_local_network_workers(timeout=2.0)
            self.assertFalse(peer_workers.local_network_workers_running())
        leftover = self._wait_for_no_generation_threads(timeout=2.0)
        self.assertEqual([], [thread.name for thread in leftover])

    def test_second_stop_after_retirement_is_a_harmless_no_op(self) -> None:
        self._start()
        peer_workers.stop_local_network_workers(timeout=2.0)
        peer_workers.stop_local_network_workers(timeout=2.0)
        self.assertFalse(peer_workers.local_network_workers_running())
        leftover = self._wait_for_no_generation_threads(timeout=2.0)
        self.assertEqual([], [thread.name for thread in leftover])


if __name__ == "__main__":
    unittest.main()
