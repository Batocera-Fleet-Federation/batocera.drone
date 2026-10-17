"""Issue #305: watchdog passes must recover from transient tailnet errors.

The watchdog is a long-lived repair loop. A transient failure in any operation
within one pass must leave later ticks available to inspect and repair state.
These tests run the captured loop synchronously with deterministic wakeups.
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.transfer import local_network, peer_workers


class TailnetWatchdogExceptionRecoveryTests(unittest.TestCase):
    def _run_watchdog(self, statuses, waits, ensure=None, repair=None):
        threads = []

        def capture_thread(*args, **kwargs):
            threads.append(kwargs)
            return mock.Mock()

        def next_wait(stop, interval):
            return next(waits)

        with mock.patch.object(peer_workers, "Thread", side_effect=capture_thread), \
                mock.patch.object(peer_workers, "_wait_for_generation_stop", side_effect=next_wait), \
                mock.patch.object(peer_workers, "tailnet_status", side_effect=statuses) as status, \
                mock.patch.object(peer_workers, "ensure_tailnet_networking", side_effect=ensure) as ensure_mock, \
                mock.patch.object(peer_workers, "_repair_tailnet_data_path", side_effect=repair) as repair_mock, \
                mock.patch.object(local_network, "start_discovery_worker", return_value=mock.Mock()), \
                mock.patch.object(peer_workers, "DroneCertificateManager"):
            peer_workers._start_local_network_workers(mock.Mock())
            watchdog = next(item["target"] for item in threads
                            if item.get("name") == "drone-tailnet-watchdog")
            with mock.patch("sys.stderr"):
                watchdog()
            peer_workers.stop_local_network_workers(timeout=1.0)
        return status, ensure_mock, repair_mock

    def test_status_exception_does_not_prevent_next_tick_repair(self):
        status, ensure, repair = self._run_watchdog(
            [RuntimeError("status unavailable"), {"running": True}],
            iter([False, False, True]),
        )
        self.assertEqual(status.call_count, 2)
        ensure.assert_not_called()
        repair.assert_called_once_with()

    def test_reenrollment_exception_does_not_prevent_next_tick(self):
        status, ensure, repair = self._run_watchdog(
            [{"running": False}, {"running": False}],
            iter([False, False, True]),
            ensure=[RuntimeError("enrollment unavailable"), None],
        )
        self.assertEqual(status.call_count, 2)
        self.assertEqual(ensure.call_count, 2)
        repair.assert_not_called()

    def test_repair_exception_does_not_prevent_next_repair(self):
        status, ensure, repair = self._run_watchdog(
            [{"running": True}, {"running": True}],
            iter([False, False, True]),
            repair=[RuntimeError("data path unavailable"), None],
        )
        self.assertEqual(status.call_count, 2)
        ensure.assert_not_called()
        self.assertEqual(repair.call_count, 2)


if __name__ == "__main__":
    unittest.main()
