"""Issue #237 adversarial UAT: real overlapping start/stop callers leave no orphans.

Expected behaviour is derived from the issue, not from the implementation:
after any interleaving of ``_start_local_network_workers`` and
``stop_local_network_workers`` calls has *completed*,

* at most one generation is live, and it is the one the module tracks;
* once the final stop returns, every thread any generation spawned (discovery,
  tailnet watchdog, tailnet bootstrap retry, health loop) exits;
* a start that fails midway does not wedge the lifecycle for later callers.

Unlike mock-only tests these use real threads and a ``Barrier`` so callers
actually overlap.  Discovery is a fake thread (no sockets/multicast); tailnet
calls are patched; the health loop's real ``time.sleep`` is shortened so it
can observe its stop event quickly.
"""

from __future__ import annotations

import threading
import time
import types
import unittest
from unittest import mock

from app.transfer import local_network, peer_workers

_REAL_SLEEP = time.sleep
_FAKE_DISCOVERY_NAME = "fake-local-discovery"
_WORKER_NAMES = (
    "drone-tailnet-networking",
    "drone-tailnet-watchdog",
    "drone-tailnet-bootstrap-retry",
    "drone-local-peer-health",
    _FAKE_DISCOVERY_NAME,
)


class _FakeDiscovery(threading.Thread):
    """Stands in for the real discovery thread: loops until stopped or closed."""

    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__(name=_FAKE_DISCOVERY_NAME, daemon=True)
        self._stop_event = stop_event
        self._closed = threading.Event()

    def close_discovery_socket(self) -> None:
        self._closed.set()

    def run(self) -> None:
        # Mirrors the real worker: only the generation event or a closed socket
        # ends it, so a leaked (never stopped/closed) handle stays alive.
        while not (self._stop_event.is_set() or self._closed.is_set()):
            _REAL_SLEEP(0.005)


def _live_workers(baseline: set) -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread not in baseline and thread.name in _WORKER_NAMES and thread.is_alive()
    ]


def _live_discovery(baseline: set) -> list[threading.Thread]:
    return [t for t in _live_workers(baseline) if t.name == _FAKE_DISCOVERY_NAME]


def _wait_until_no_workers(baseline: set, timeout: float = 5.0) -> list[threading.Thread]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _live_workers(baseline):
            return []
        _REAL_SLEEP(0.01)
    return _live_workers(baseline)


class RealThreadLifecycleStormUAT(unittest.TestCase):
    def setUp(self) -> None:
        peer_workers.stop_local_network_workers(timeout=1.0)
        self.baseline = set(threading.enumerate())
        self.created: list[_FakeDiscovery] = []
        self.created_lock = threading.Lock()
        self.fail_next_discovery = threading.Event()

        def start_discovery(settings, fingerprint, on_discovery=None, stop_event=None):
            if self.fail_next_discovery.is_set():
                self.fail_next_discovery.clear()
                raise OSError("simulated multicast bind failure")
            worker = _FakeDiscovery(stop_event)
            worker.start()
            with self.created_lock:
                self.created.append(worker)
            return worker

        fast_time = types.SimpleNamespace(
            sleep=lambda seconds: _REAL_SLEEP(0.005),
            monotonic=time.monotonic,
        )
        patches = [
            mock.patch.object(peer_workers, "ensure_tailnet_networking"),
            mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True}),
            mock.patch.object(peer_workers, "_repair_tailnet_data_path"),
            mock.patch.object(peer_workers, "time", fast_time),
            mock.patch.object(local_network, "is_local_mode", return_value=False),
            mock.patch.object(local_network, "start_discovery_worker", side_effect=start_discovery),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._final_cleanup)

    def _final_cleanup(self) -> None:
        peer_workers.stop_local_network_workers(timeout=1.0)
        _wait_until_no_workers(self.baseline, timeout=3.0)

    def _run_barrier(self, count: int, body) -> list[BaseException]:
        barrier = threading.Barrier(count)
        errors: list[BaseException] = []

        def runner(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                body(index)
            except BaseException as error:  # surfaced by the caller's assertion
                errors.append(error)

        threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse([t for t in threads if t.is_alive()], "a lifecycle caller deadlocked")
        return errors

    def test_barrier_released_starts_leave_exactly_one_tracked_live_generation(self) -> None:
        for round_number in range(5):
            with self.subTest(round=round_number):
                errors = self._run_barrier(
                    8, lambda _i: peer_workers._start_local_network_workers(mock.Mock())
                )
                self.assertEqual(errors, [])

                live = _live_discovery(self.baseline)
                self.assertEqual(len(live), 1, f"expected one live discovery thread, got {len(live)}")
                self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, live[0])
                self.assertFalse(peer_workers._LOCAL_NETWORK_STOP.is_set())

                peer_workers.stop_local_network_workers(timeout=2.0)
                self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)
                self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
                leaked = _wait_until_no_workers(self.baseline)
                self.assertEqual([t.name for t in leaked], [], "threads survived the final stop")

    def test_mixed_start_stop_storm_ends_clean_after_final_stop(self) -> None:
        def body(index: int) -> None:
            settings = mock.Mock()
            for step in range(4):
                if (index + step) % 3 == 0:
                    peer_workers.stop_local_network_workers(timeout=2.0)
                else:
                    peer_workers._start_local_network_workers(settings)

        for round_number in range(5):
            with self.subTest(round=round_number):
                errors = self._run_barrier(8, body)
                self.assertEqual(errors, [])

                # Quiescent invariant: nothing but the tracked generation is live.
                tracked = peer_workers._LOCAL_NETWORK_DISCOVERY
                live = _live_discovery(self.baseline)
                if tracked is None:
                    self.assertEqual(live, [], "discovery live while module tracks none")
                else:
                    self.assertEqual(live, [tracked], "an untracked discovery thread is live")

                peer_workers.stop_local_network_workers(timeout=2.0)
                leaked = _wait_until_no_workers(self.baseline)
                self.assertEqual([t.name for t in leaked], [], "threads survived the final stop")
                self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)
                self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)

    def test_every_superseded_generation_event_is_set(self) -> None:
        events: list[threading.Event] = []
        events_lock = threading.Lock()
        real_event = threading.Event

        def recording_event():
            event = real_event()
            with events_lock:
                events.append(event)
            return event

        with mock.patch.object(peer_workers, "Event", side_effect=recording_event):
            errors = self._run_barrier(
                6, lambda _i: peer_workers._start_local_network_workers(mock.Mock())
            )
            self.assertEqual(errors, [])
            live_event = peer_workers._LOCAL_NETWORK_STOP
            others = [e for e in events if e is not live_event]
            self.assertEqual(len(events), 6)
            self.assertTrue(all(e.is_set() for e in others), "a superseded generation was never signalled")
            self.assertFalse(live_event.is_set())
        peer_workers.stop_local_network_workers(timeout=2.0)
        self.assertEqual([t.name for t in _wait_until_no_workers(self.baseline)], [])

    def test_failed_start_does_not_wedge_concurrent_or_later_callers(self) -> None:
        peer_workers._start_local_network_workers(mock.Mock())
        self.fail_next_discovery.set()
        outcomes: list[str] = []
        outcomes_lock = threading.Lock()

        def body(_index: int) -> None:
            try:
                peer_workers._start_local_network_workers(mock.Mock())
                result = "ok"
            except OSError:
                result = "failed"
            with outcomes_lock:
                outcomes.append(result)

        errors = self._run_barrier(4, body)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(outcomes), ["failed", "ok", "ok", "ok"])

        # A later start must succeed and leave a single tracked live generation.
        peer_workers._start_local_network_workers(mock.Mock())
        live = _live_discovery(self.baseline)
        self.assertEqual(live, [peer_workers._LOCAL_NETWORK_DISCOVERY])

        peer_workers.stop_local_network_workers(timeout=2.0)
        leaked = _wait_until_no_workers(self.baseline)
        self.assertEqual([t.name for t in leaked], [], "failed-start generation leaked threads")

    def test_failed_start_generation_is_retired_by_the_next_stop(self) -> None:
        self.fail_next_discovery.set()
        with self.assertRaises(OSError):
            peer_workers._start_local_network_workers(mock.Mock())
        # Watchdog/bootstrap threads of the half-started generation exist now;
        # stop must still signal them even though no discovery handle was published.
        peer_workers.stop_local_network_workers(timeout=1.0)
        leaked = _wait_until_no_workers(self.baseline)
        self.assertEqual([t.name for t in leaked], [], "half-started generation was orphaned")
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)

    def test_stop_while_start_blocked_in_discovery_does_not_leave_live_handle(self) -> None:
        """A stop issued mid-start must not return leaving a live, untracked worker."""
        entered = threading.Event()
        release = threading.Event()
        real_factory = local_network.start_discovery_worker

        def slow_factory(*args, **kwargs):
            entered.set()
            release.wait(5)
            return real_factory.side_effect(*args, **kwargs)

        with mock.patch.object(local_network, "start_discovery_worker", side_effect=slow_factory):
            starter = threading.Thread(target=lambda: peer_workers._start_local_network_workers(mock.Mock()))
            starter.start()
            self.assertTrue(entered.wait(3))
            stopper = threading.Thread(target=lambda: peer_workers.stop_local_network_workers(timeout=2.0))
            stopper.start()
            _REAL_SLEEP(0.2)
            release.set()
            starter.join(5)
            stopper.join(5)
            self.assertFalse(starter.is_alive() or stopper.is_alive())

        # Whichever order the two serialized in, nothing may be live and untracked.
        tracked = peer_workers._LOCAL_NETWORK_DISCOVERY
        live = _live_discovery(self.baseline)
        self.assertEqual(live, [] if tracked is None else [tracked])
        peer_workers.stop_local_network_workers(timeout=2.0)
        self.assertEqual([t.name for t in _wait_until_no_workers(self.baseline)], [])


if __name__ == "__main__":
    unittest.main()
