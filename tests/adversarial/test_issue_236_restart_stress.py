"""Issue #236 adversarial stress: generations of tailnet watchdog/bootstrap loops.

Invariants derived from the report (not from the implementation):

- N rapid restarts leave exactly one watchdog and one bootstrap-retry thread.
- A generation whose startup failed part-way (discovery raised) is still retired by
  the next stop or restart; its loops must not outlive it.
- A restart while an old loop is mid-iteration lets that iteration finish but the old
  loop does no further work, while the new generation keeps working.
- Concurrent stop calls are safe, and stop is idempotent.

Real Thread targets are used. Only the wait is shortened (keeping the generation
event's stop semantics) so wake-ups happen in milliseconds; Tailscale, peer and
discovery calls are patched out.
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from unittest import mock

from app.transfer import local_network, peer_workers


WATCHDOG = "drone-tailnet-watchdog"
BOOTSTRAP = "drone-tailnet-bootstrap-retry"
TAILNET_LOOPS = (WATCHDOG, BOOTSTRAP)


class TailnetRestartStressTests(unittest.TestCase):
    def setUp(self) -> None:
        self._existing = set(threading.enumerate())
        self._real_sleep = time.sleep
        peer_workers.stop_local_network_workers(timeout=1.0)
        env = mock.patch.dict(os.environ, {
            "DRONE_TAILNET_WATCHDOG_INTERVAL_SECONDS": "30",
            "DRONE_TAILNET_BOOTSTRAP_INTERVAL_SECONDS": "60",
            "DRONE_LOCAL_HEALTH_INTERVAL_SECONDS": "30",
        })
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._teardown_generation)

    def _teardown_generation(self) -> None:
        peer_workers.stop_local_network_workers(timeout=1.0)
        for name in TAILNET_LOOPS:
            self._settle(name, 0)

    def _live(self, name: str) -> list[threading.Thread]:
        return [
            t for t in threading.enumerate()
            if t.name == name and t.is_alive() and t not in self._existing
        ]

    def _settle(self, name: str, expected: int, within: float = 5.0) -> list[threading.Thread]:
        deadline = time.monotonic() + within
        live = self._live(name)
        while len(live) != expected and time.monotonic() < deadline:
            self._real_sleep(0.01)
            live = self._live(name)
        return live

    def _patch(self, target: object, attr: str, **kwargs: object) -> mock.Mock:
        patcher = mock.patch.object(target, attr, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def _fast_wait(self) -> None:
        """Poll quickly but keep the event-based stop semantics of the real helper."""
        real_wait = peer_workers._wait_for_generation_stop
        self._patch(
            peer_workers, "_wait_for_generation_stop",
            side_effect=lambda stop, interval: real_wait(stop, 0.01),
        )

    def _quiet(self) -> dict[str, mock.Mock]:
        return {
            "ensure": self._patch(peer_workers, "ensure_tailnet_networking"),
            "status": self._patch(peer_workers, "tailnet_status", return_value={"running": True}),
            "repair": self._patch(peer_workers, "_repair_tailnet_data_path"),
            "bootstrap": self._patch(peer_workers, "bootstrap_tailnet_from_swarm"),
            "discovery": self._patch(local_network, "start_discovery_worker", return_value=mock.Mock()),
            "local_mode": self._patch(local_network, "is_local_mode", return_value=False),
        }

    def test_many_rapid_restarts_leave_exactly_one_of_each(self) -> None:
        self._quiet()
        self._fast_wait()
        for _ in range(25):
            peer_workers._start_local_network_workers(mock.Mock())
        for name in TAILNET_LOOPS:
            self.assertEqual(len(self._settle(name, 1)), 1, name)
        peer_workers.stop_local_network_workers(timeout=1.0)
        for name in TAILNET_LOOPS:
            self.assertEqual(len(self._settle(name, 0)), 0, name)

    def test_real_interval_threads_do_not_stack_across_restarts(self) -> None:
        # No wait patch: the retired loops are blocked in their real 30 s / 60 s wait.
        self._quiet()
        for _ in range(5):
            peer_workers._start_local_network_workers(mock.Mock())
        for name in TAILNET_LOOPS:
            self.assertEqual(len(self._settle(name, 1, within=2.0)), 1, name)
        peer_workers.stop_local_network_workers(timeout=1.0)
        for name in TAILNET_LOOPS:
            self.assertEqual(len(self._settle(name, 0, within=2.0)), 0, name)

    def test_partially_started_generation_is_retired_by_next_stop(self) -> None:
        self._fast_wait()
        self._quiet()["discovery"].side_effect = RuntimeError("bind failed")
        with self.assertRaisesRegex(RuntimeError, "bind failed"):
            peer_workers._start_local_network_workers(mock.Mock())
        # Both loops were already started before discovery failed.
        for name in TAILNET_LOOPS:
            self.assertEqual(len(self._settle(name, 1)), 1, name)
        peer_workers.stop_local_network_workers(timeout=1.0)
        for name in TAILNET_LOOPS:
            self.assertEqual(len(self._settle(name, 0)), 0, f"{name} leaked after failed start")

    def test_partially_started_generation_is_retired_by_successful_restart(self) -> None:
        self._fast_wait()
        mocks = self._quiet()
        mocks["discovery"].side_effect = [RuntimeError("bind failed"), mock.Mock()]
        with self.assertRaises(RuntimeError):
            peer_workers._start_local_network_workers(mock.Mock())
        peer_workers._start_local_network_workers(mock.Mock())
        for name in TAILNET_LOOPS:
            self.assertEqual(len(self._settle(name, 1)), 1, name)

    def test_restart_during_iteration_old_loop_finishes_once_then_exits(self) -> None:
        self._fast_wait()
        in_work = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def blocking_ensure(settings: object) -> None:
            name = threading.current_thread().name
            calls.append(name)
            if name == WATCHDOG and not in_work.is_set():
                in_work.set()
                release.wait(5)

        mocks = self._quiet()
        mocks["status"].return_value = {"running": False, "installed": False}
        mocks["ensure"].side_effect = blocking_ensure
        peer_workers._start_local_network_workers(mock.Mock())
        self.assertTrue(in_work.wait(5), "first watchdog never entered its iteration")
        old_watchdog = self._live(WATCHDOG)[0]

        peer_workers._start_local_network_workers(mock.Mock())
        release.set()
        old_watchdog.join(5)
        self.assertFalse(old_watchdog.is_alive(), "retired watchdog kept looping")
        self.assertEqual(len(self._settle(WATCHDOG, 1)), 1)

        # The retired loop contributes no further work; the new one keeps working.
        self._real_sleep(0.1)
        baseline = len(calls)
        self._real_sleep(0.2)
        self.assertGreater(len(calls), baseline, "new generation stopped doing work")

    def test_new_generation_keeps_working_after_old_one_retired(self) -> None:
        self._fast_wait()
        mocks = self._quiet()
        peer_workers._start_local_network_workers(mock.Mock())
        peer_workers._start_local_network_workers(mock.Mock())
        for name in TAILNET_LOOPS:
            self.assertEqual(len(self._settle(name, 1)), 1, name)
        mocks["repair"].reset_mock()
        deadline = time.monotonic() + 3
        while not mocks["repair"].called and time.monotonic() < deadline:
            self._real_sleep(0.01)
        self.assertTrue(mocks["repair"].called, "surviving watchdog does no work")

    def test_bootstrap_loop_stays_quiet_once_stopped(self) -> None:
        self._fast_wait()
        mocks = self._quiet()
        mocks["status"].return_value = {"running": True, "installed": True, "enrolled": False}
        peer_workers._start_local_network_workers(mock.Mock())
        deadline = time.monotonic() + 3
        while not mocks["bootstrap"].called and time.monotonic() < deadline:
            self._real_sleep(0.01)
        self.assertTrue(mocks["bootstrap"].called, "bootstrap retry never ran while active")
        peer_workers.stop_local_network_workers(timeout=1.0)
        self.assertEqual(len(self._settle(BOOTSTRAP, 0)), 0)
        count = mocks["bootstrap"].call_count
        self._real_sleep(0.2)
        self.assertEqual(mocks["bootstrap"].call_count, count, "bootstrap ran after stop")

    def test_concurrent_and_repeated_stops_are_safe(self) -> None:
        self._fast_wait()
        self._quiet()
        peer_workers._start_local_network_workers(mock.Mock())
        errors: list[BaseException] = []

        def stopper() -> None:
            try:
                peer_workers.stop_local_network_workers(timeout=1.0)
            except BaseException as error:  # pragma: no cover - failure path
                errors.append(error)

        threads = [threading.Thread(target=stopper) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(errors, [])
        for name in TAILNET_LOOPS:
            self.assertEqual(len(self._settle(name, 0)), 0, name)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)


if __name__ == "__main__":
    unittest.main()
