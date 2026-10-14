"""Issue #236 adversarial boundaries: tailnet watchdog and bootstrap generations.

Expected behaviour is taken from the report, not from the implementation:

- After a restart, exactly one watchdog and one bootstrap-retry thread are alive.
- After a stop, neither thread is alive, and a stopped loop does no tailnet work.
- Stop and restart must be prompt: a retry loop must not keep a retired generation
  alive for a whole poll interval (60 s watchdog, 300 s bootstrap by default).
- Stop without a start is a no-op, and the discovery handle is closed and joined
  before the next generation starts (related #210 regression).

The first two tests use the real ``time.sleep`` and the real interval floor, so they
measure the actual lifetime of the threads. Other tests gate the wake-up so the
ordering is deterministic. Network, Tailscale, and discovery are patched out.
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
PROMPT_SECONDS = 2.0


class TailnetGenerationBoundariesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._existing_threads = set(threading.enumerate())
        self._real_sleep = time.sleep
        peer_workers.stop_local_network_workers(timeout=1.0)
        env = mock.patch.dict(os.environ, {
            "DRONE_TAILNET_WATCHDOG_INTERVAL_SECONDS": "30",
            "DRONE_TAILNET_BOOTSTRAP_INTERVAL_SECONDS": "60",
            "DRONE_LOCAL_HEALTH_INTERVAL_SECONDS": "30",
        })
        env.start()
        self.addCleanup(env.stop)

    def _live(self, name: str) -> list[threading.Thread]:
        # Only threads created by this test; leftovers from earlier tests are excluded.
        return [
            thread
            for thread in threading.enumerate()
            if thread.name == name and thread.is_alive() and thread not in self._existing_threads
        ]

    def _settle(self, name: str, expected: int, within: float = PROMPT_SECONDS) -> list[threading.Thread]:
        deadline = time.monotonic() + within
        live = self._live(name)
        while len(live) != expected and time.monotonic() < deadline:
            self._real_sleep(0.02)
            live = self._live(name)
        return live

    def _patched_workers(self, **extra: object):
        """Patch every outbound tailnet/discovery call and return the mocks."""
        patches = {
            "ensure_tailnet_networking": mock.patch.object(peer_workers, "ensure_tailnet_networking"),
            "tailnet_status": mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True}),
            "repair": mock.patch.object(peer_workers, "_repair_tailnet_data_path"),
            "bootstrap": mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"),
            "discovery": mock.patch.object(local_network, "start_discovery_worker", return_value=mock.Mock()),
            # Health loop is inert: no paired peers are probed.
            "local_mode": mock.patch.object(local_network, "is_local_mode", return_value=False),
        }
        patches.update(extra)
        started = {}
        for key, patcher in patches.items():
            started[key] = patcher.start()
            self.addCleanup(patcher.stop)
        return started

    # --- real-time boundaries (no sleep patch) -------------------------------------

    def test_restart_leaves_one_watchdog_and_one_bootstrap_promptly(self) -> None:
        self._patched_workers()
        peer_workers._start_local_network_workers(mock.Mock())
        for name in TAILNET_LOOPS:
            self.assertEqual(len(self._settle(name, 1)), 1, name)

        # The retired generation is still sleeping; it must exit without waiting its interval.
        peer_workers._start_local_network_workers(mock.Mock())
        for name in TAILNET_LOOPS:
            live = self._settle(name, 1)
            self.assertEqual(len(live), 1, f"{name} alive after restart: {len(live)}")

    def test_stop_retires_both_loops_without_waiting_a_full_interval(self) -> None:
        self._patched_workers()
        peer_workers._start_local_network_workers(mock.Mock())
        for name in TAILNET_LOOPS:
            self.assertEqual(len(self._settle(name, 1)), 1, name)

        peer_workers.stop_local_network_workers(timeout=1.0)
        for name in TAILNET_LOOPS:
            live = self._settle(name, 0)
            self.assertEqual(len(live), 0, f"{name} still alive after stop: {len(live)}")

    # --- deterministic wake-up ordering -------------------------------------------

    def test_stop_during_sleep_performs_no_tailnet_work_when_wake_arrives(self) -> None:
        release = threading.Event()
        real_sleep = self._real_sleep

        def gated_sleep(seconds: float) -> None:
            # Tailnet loops block until the test releases them; other loops run briefly.
            if threading.current_thread().name in TAILNET_LOOPS:
                release.wait(5)
            else:
                real_sleep(0.01)

        mocks = self._patched_workers()
        with mock.patch.object(peer_workers.time, "sleep", side_effect=gated_sleep):
            peer_workers._start_local_network_workers(mock.Mock())
            self._settle(WATCHDOG, 1)
            self._settle(BOOTSTRAP, 1)
            # The one-shot startup networking thread legitimately calls ensure once; wait for it.
            self._settle("drone-tailnet-networking", 0)
            mocks["ensure_tailnet_networking"].reset_mock()
            peer_workers.stop_local_network_workers(timeout=1.0)
            release.set()
            for name in TAILNET_LOOPS:
                self.assertEqual(len(self._settle(name, 0)), 0, name)

        mocks["tailnet_status"].assert_not_called()
        mocks["ensure_tailnet_networking"].assert_not_called()
        mocks["repair"].assert_not_called()
        mocks["bootstrap"].assert_not_called()

    def test_stop_with_no_started_generation_is_noop_and_resets_state(self) -> None:
        peer_workers.stop_local_network_workers()
        peer_workers.stop_local_network_workers()
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)

    def test_restart_closes_and_joins_previous_discovery_before_new_generation(self) -> None:
        # Related #210 regression: the old discovery handle must be retired before
        # the new generation's discovery worker starts.
        events: list[str] = []
        first = mock.Mock()
        first.close_discovery_socket.side_effect = lambda: events.append("close")
        first.join.side_effect = lambda timeout=None: events.append("join")
        second = mock.Mock()
        handles = [first, second]

        def start_discovery(*args: object, **kwargs: object) -> mock.Mock:
            events.append("start")
            return handles.pop(0)

        self._patched_workers(discovery=mock.patch.object(
            local_network, "start_discovery_worker", side_effect=start_discovery,
        ))
        peer_workers._start_local_network_workers(mock.Mock())
        peer_workers._start_local_network_workers(mock.Mock())
        self.assertEqual(events, ["start", "close", "join", "start"])
        self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, second)

    def test_interval_floors_are_applied_to_tailnet_loops(self) -> None:
        # DRONE_* intervals below the floor are clamped: watchdog 30 s, bootstrap 60 s.
        recorded: dict[str, list[float]] = {WATCHDOG: [], BOOTSTRAP: []}
        real_sleep = self._real_sleep

        def record_first_sleep(seconds: float) -> None:
            # Only the first requested interval per loop is recorded; the loop then keeps
            # running on a short real sleep until the test stops the generation.
            name = threading.current_thread().name
            if name in recorded and not recorded[name]:
                recorded[name].append(seconds)
            real_sleep(0.01)

        self._patched_workers()
        with mock.patch.dict(os.environ, {
            "DRONE_TAILNET_WATCHDOG_INTERVAL_SECONDS": "0",
            "DRONE_TAILNET_BOOTSTRAP_INTERVAL_SECONDS": "1",
        }), mock.patch.object(peer_workers.time, "sleep", side_effect=record_first_sleep):
            peer_workers._start_local_network_workers(mock.Mock())
            deadline = time.monotonic() + 5.0
            while not (recorded[WATCHDOG] and recorded[BOOTSTRAP]) and time.monotonic() < deadline:
                real_sleep(0.01)
            peer_workers.stop_local_network_workers(timeout=1.0)
            for name in TAILNET_LOOPS:
                self.assertEqual(len(self._settle(name, 0, within=5.0)), 0, name)
        self.assertEqual(recorded[WATCHDOG], [30])
        self.assertEqual(recorded[BOOTSTRAP], [60])


if __name__ == "__main__":
    unittest.main()
