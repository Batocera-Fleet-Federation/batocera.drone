"""UAT for #586: the sharing-revocation poller as it runs on its real daemon thread.

Issue #586: run_sharing_revocation_poller has no guard around
check_sharing_revocation, so one unexpected exception ends the forever-loop.
Expected behavior, derived from the issue rather than the implementation:

* A failing tick is reported and the next tick still runs.
* A successful tick (including one that revokes) is never reported as a failure.
* Retirement by generation still ends the thread promptly and no check runs
  after it, even when earlier ticks failed.
* The thread ends because it was retired, not because an exception escaped it
  (threading.excepthook must stay silent).

The fake clock and the patched poller_retired keep this deterministic: no real
sleeping and no mutation of the process-wide poller generation.
"""

from __future__ import annotations

import io
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class _NonStringStrError(Exception):
    def __str__(self) -> str:  # type: ignore[override]
        return 42  # type: ignore[return-value]


class Issue586PollerThreadLifecycleUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install-root")
        self.addCleanup(patcher.stop)
        patcher.start()
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-586-poller-thread",
        }, clear=True):
            self.settings = Settings.from_env()

    def _run_on_thread(self, outcomes: list) -> tuple[list, io.StringIO, threading.Thread, list]:
        """Run the real poller on a daemon thread; ``outcomes`` drive each tick's check.

        Returns (calls, captured stderr, thread, uncaught thread exceptions). The
        poller is retired once every outcome has been consumed.
        """
        calls: list = []
        uncaught: list = []

        def check(_settings: Settings) -> bool:
            calls.append(None)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def fake_retired(_generation: int) -> bool:
            return len(calls) >= len(outcomes)

        fake_time = types.SimpleNamespace(sleep=lambda _seconds: None)
        stderr = io.StringIO()
        with mock.patch.object(vpn_manager, "time", fake_time), \
                mock.patch.object(vpn_manager, "poller_generation", return_value=11), \
                mock.patch.object(vpn_manager, "poller_retired", side_effect=fake_retired), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", side_effect=check), \
                mock.patch.object(sys, "stderr", stderr), \
                mock.patch.object(threading, "excepthook", side_effect=uncaught.append):
            thread = threading.Thread(
                target=vpn_manager.run_sharing_revocation_poller,
                args=(self.settings,),
                name="drone-vpn-sharing-revocation-test",
                daemon=True,
            )
            thread.start()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "poller thread did not exit after retirement")
        return calls, stderr, thread, uncaught

    def test_real_thread_survives_failures_and_exits_only_on_retirement(self) -> None:
        outcomes = [RuntimeError("tick one"), False, ValueError("tick three"), False]

        calls, stderr, _thread, uncaught = self._run_on_thread(outcomes)

        self.assertEqual(len(calls), len(outcomes), "every tick before retirement must run its check")
        self.assertEqual(uncaught, [], "a tick failure must not escape the poller thread")
        report = stderr.getvalue()
        self.assertIn("RuntimeError: tick one", report)
        self.assertIn("ValueError: tick three", report)

    def test_only_failed_ticks_are_reported(self) -> None:
        # Tick 2 revokes (True) and tick 4 is quiet; neither may be logged as a failure.
        outcomes = [False, True, RuntimeError("only failure"), False]

        calls, stderr, _thread, uncaught = self._run_on_thread(outcomes)

        self.assertEqual(len(calls), len(outcomes))
        self.assertEqual(uncaught, [])
        lines = [line for line in stderr.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"expected one failure report, got: {lines!r}")
        self.assertIn("only failure", lines[0])

    def test_non_string_exception_text_still_lets_later_ticks_run(self) -> None:
        # str() on this exception raises TypeError; the report must not stop the poller.
        outcomes = [_NonStringStrError(), False, False]

        calls, _stderr, _thread, uncaught = self._run_on_thread(outcomes)

        self.assertEqual(len(calls), len(outcomes), "a non-string __str__ must not stop later ticks")
        self.assertEqual(uncaught, [])

    def test_keyboard_interrupt_on_thread_ends_the_poller_instead_of_being_absorbed(self) -> None:
        # Shutdown signals are not tick failures: the check must not be retried.
        outcomes = [KeyboardInterrupt(), False]

        with self.assertRaises(KeyboardInterrupt):
            fake_time = types.SimpleNamespace(sleep=lambda _seconds: None)
            calls: list = []

            def check(_settings: Settings) -> bool:
                calls.append(None)
                raise outcomes[0]

            with mock.patch.object(vpn_manager, "time", fake_time), \
                    mock.patch.object(vpn_manager, "poller_generation", return_value=11), \
                    mock.patch.object(vpn_manager, "poller_retired", return_value=False), \
                    mock.patch.object(vpn_manager, "check_sharing_revocation", side_effect=check):
                vpn_manager.run_sharing_revocation_poller(self.settings)

        self.assertEqual(len(calls), 1, "a KeyboardInterrupt must not trigger a second check")


if __name__ == "__main__":
    unittest.main()
