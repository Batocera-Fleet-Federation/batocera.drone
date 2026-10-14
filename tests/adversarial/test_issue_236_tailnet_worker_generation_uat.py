"""Issue #236 UAT: tailnet retry loops must be owned by one worker generation.

The lifecycle contract is derived from the report: consecutive calls to
``_start_local_network_workers`` must retire the first watchdog and bootstrap
generation, and a later stop must retire the replacement as well.  The test
uses real daemon threads for those loops; unrelated start-up and discovery
workers are inert handles so no network or device service is required.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from app.transfer import local_network, peer_workers


WATCHDOG = "drone-tailnet-watchdog"
BOOTSTRAP = "drone-tailnet-bootstrap-retry"
TAILNET_LOOPS = (WATCHDOG, BOOTSTRAP)


class TailnetWorkerGenerationUAT(unittest.TestCase):
    def setUp(self) -> None:
        self._existing_threads = set(threading.enumerate())
        self._real_sleep = time.sleep
        peer_workers.stop_local_network_workers(timeout=1.0)

    def tearDown(self) -> None:
        peer_workers.stop_local_network_workers(timeout=1.0)
        for name in TAILNET_LOOPS:
            self._wait_for_live_count(name, 0)

    def _live_threads(self, name: str) -> list[threading.Thread]:
        return [
            thread
            for thread in threading.enumerate()
            if thread.name == name and thread.is_alive() and thread not in self._existing_threads
        ]

    def _wait_for_live_count(self, name: str, expected: int) -> list[threading.Thread]:
        deadline = time.monotonic() + 5.0
        live = self._live_threads(name)
        while len(live) != expected and time.monotonic() < deadline:
            self._real_sleep(0.01)
            live = self._live_threads(name)
        self.assertEqual(len(live), expected, (name, [thread.ident for thread in live]))
        return live

    @staticmethod
    def _inert_thread(*args: object, **kwargs: object) -> mock.Mock:
        thread = mock.Mock()
        thread.name = kwargs.get("name", "")
        thread.start = lambda: None
        thread.is_alive.return_value = False
        return thread

    def _thread_factory(self, *args: object, **kwargs: object) -> threading.Thread | mock.Mock:
        if kwargs.get("name") in TAILNET_LOOPS:
            return threading.Thread(*args, **kwargs)
        return self._inert_thread(*args, **kwargs)

    def test_restart_and_stop_retire_watchdog_and_bootstrap_generations(self) -> None:
        """A restarted or stopped generation cannot leave either retry loop alive."""
        discovery = mock.Mock()
        # The loops wait on the generation event, so shorten that wait (keeping its
        # stop semantics) to cross the wake boundary without a 60 s / 300 s interval.
        real_wait = peer_workers._wait_for_generation_stop
        with mock.patch.object(peer_workers, "Thread", side_effect=self._thread_factory), \
                mock.patch.object(peer_workers, "_wait_for_generation_stop",
                                  side_effect=lambda stop, _interval: real_wait(stop, 0.005)), \
                mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
                mock.patch.object(peer_workers, "tailnet_status", return_value={
                    "running": True, "installed": True, "enrolled": False,
                }), \
                mock.patch.object(peer_workers, "_repair_tailnet_data_path") as repair, \
                mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm") as bootstrap, \
                mock.patch.object(local_network, "start_discovery_worker", return_value=discovery):
            peer_workers._start_local_network_workers(mock.Mock())
            first_generation = {
                name: self._wait_for_live_count(name, 1)[0] for name in TAILNET_LOOPS
            }

            # Both loops have actually crossed their wake boundary; this avoids
            # passing merely because a thread was created but never executed.
            deadline = time.monotonic() + 5.0
            while (not repair.called or not bootstrap.called) and time.monotonic() < deadline:
                self._real_sleep(0.01)
            self.assertTrue(repair.called)
            self.assertTrue(bootstrap.called)

            peer_workers._start_local_network_workers(mock.Mock())
            for name, first in first_generation.items():
                live = self._wait_for_live_count(name, 1)
                self.assertIsNot(live[0], first)
                self.assertFalse(first.is_alive(), name)

            peer_workers.stop_local_network_workers(timeout=1.0)
            for name in TAILNET_LOOPS:
                self._wait_for_live_count(name, 0)

            # Stopped loops must no longer execute background repair/re-enroll work.
            repair_calls = repair.call_count
            bootstrap_calls = bootstrap.call_count
            self._real_sleep(0.05)
            self.assertEqual(repair.call_count, repair_calls)
            self.assertEqual(bootstrap.call_count, bootstrap_calls)


if __name__ == "__main__":
    unittest.main()
