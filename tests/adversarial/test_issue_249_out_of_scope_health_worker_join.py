"""Out-of-scope evidence for issue #249: ``stop_local_network_workers`` does not join the health worker.

This suite is deliberately separate from the in-scope #249 suites. Issue #249 is about
adversarial *harnesses* calling ``stop_local_network_workers`` before temp-tree removal. The
defect here is in the product helper itself: it signals the generation and joins only the
discovery thread. The paired-peer health loop (``drone-local-peer-health``) is never joined,
and it writes peer-check and membership state through the shared state DB after the helper
has already returned. A harness that removes its temp tree at that point is recreated by
``open_database`` (``mkdir(parents=True)``), which is the same symptom reported in #213.

It is kept retained and non-blocking for the scheduled run, and the failing IDs are cited in
the out-of-scope finding for the worker-lifecycle owner.

Reproduction, observed on this branch before any change:

- Full adversarial discovery (914 tests) reports failures in
  ``test_issue_191_lifecycle_boundaries`` (health loop writes after stop) and
  ``test_issue_191_teardown_uat`` / ``test_issue_212_create_server_teardown_uat`` (late
  WAL opens after rmtree). The health loop is the common writer.

Determinism: ``time.sleep`` in ``peer_workers`` is replaced so the health iteration starts
immediately. ``paired_peers`` blocks the first iteration mid-flight, so the test controls
exactly when stop returns relative to that iteration. Nothing depends on wall-clock timing.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time as real_time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.transfer import local_network, peer_workers


HEALTH_THREAD = "drone-local-peer-health"


class _Recorder:
    def __init__(self) -> None:
        self.stopped = False
        self.writes_after_stop: list[str] = []
        self.health_alive_at_stop_return: bool | None = None


# Adjudicated for #344: an unreleased health iteration cannot coexist with a
# successful join barrier. A timeout is therefore the required result, followed
# by a retry once the in-flight work is released.
class HealthWorkerStopJoinOutOfScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-249-health-")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(peer_workers.stop_local_network_workers, 3.0)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.sqlite3"
        self.entered = threading.Event()
        self.release = threading.Event()
        self.recorder = _Recorder()
        self.settings = mock.Mock()
        self.settings.device_id = "issue-249-health"
        self.settings.userdata_root = self.root

        def paired_peers(_settings):
            if not self.entered.is_set():
                self.entered.set()
                self.release.wait(10)
            return []

        def save_peer_checks(_settings, _checks):
            if self.recorder.stopped:
                self.recorder.writes_after_stop.append("save_peer_checks")

        def propagate(_settings):
            if self.recorder.stopped:
                self.recorder.writes_after_stop.append("propagate_membership")

        def fake_discovery(_settings, _fingerprint, on_discovery=None, stop_event=None):
            worker = threading.Thread(target=stop_event.wait, name="issue-249-fake-discovery", daemon=True)
            worker.close_discovery_socket = lambda: None  # type: ignore[attr-defined]
            worker.start()
            return worker

        patches = [
            mock.patch.object(peer_workers, "ensure_tailnet_networking"),
            mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"),
            mock.patch.object(
                peer_workers,
                "tailnet_status",
                return_value={"running": True, "installed": True, "enrolled": True},
            ),
            mock.patch.object(peer_workers, "_repair_tailnet_data_path"),
            mock.patch.object(peer_workers, "DroneCertificateManager"),
            # Health loop sleeps first; make that immediate so the iteration starts without waiting.
            mock.patch.object(peer_workers, "time", SimpleNamespace(sleep=lambda _s: None, monotonic=real_time.monotonic)),
            mock.patch.object(local_network, "is_local_mode", return_value=True),
            mock.patch.object(local_network, "paired_peers", side_effect=paired_peers),
            mock.patch.object(local_network, "save_peer_checks", side_effect=save_peer_checks),
            mock.patch.object(local_network, "start_discovery_worker", side_effect=fake_discovery),
            mock.patch("app.transfer.swarm_membership.propagate_membership", side_effect=propagate),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _health_thread(self) -> threading.Thread | None:
        for thread in threading.enumerate():
            if thread.name == HEALTH_THREAD and thread.is_alive():
                return thread
        return None

    def _start_with_inflight_health_iteration(self) -> threading.Thread:
        peer_workers._start_local_network_workers(self.settings)
        self.assertTrue(self.entered.wait(5), "health iteration never reached paired_peers")
        health = self._health_thread()
        self.assertIsNotNone(health, "health worker must be running before stop")
        return health

    def test_no_peer_check_or_membership_write_after_stop_returns(self) -> None:
        """An unreleased iteration must time out rather than fake a completed stop."""
        health = self._start_with_inflight_health_iteration()
        stop = peer_workers._LOCAL_NETWORK_STOP
        with self.assertRaises(TimeoutError):
            peer_workers.stop_local_network_workers(timeout=0.05)
        self.assertTrue(stop.is_set())
        self.assertTrue(health.is_alive())

        self.recorder.stopped = True
        self.release.set()
        peer_workers.stop_local_network_workers(timeout=3.0)
        health.join(timeout=5)
        self.assertEqual(self.recorder.writes_after_stop, [])

    def test_stop_returns_only_after_health_worker_has_exited(self) -> None:
        """The health worker is a LAN worker of the generation; stop must join it like discovery."""
        health = self._start_with_inflight_health_iteration()
        errors: list[BaseException] = []

        def stop() -> None:
            try:
                peer_workers.stop_local_network_workers(timeout=3.0)
            except BaseException as error:
                errors.append(error)

        stopper = threading.Thread(target=stop)
        stopper.start()
        self.release.set()
        stopper.join(timeout=5)
        self.assertFalse(stopper.is_alive(), "stop did not finish after the health worker was released")
        self.assertEqual(errors, [])
        self.assertFalse(
            health.is_alive(),
            "stop_local_network_workers returned while drone-local-peer-health was still alive",
        )
        health.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
