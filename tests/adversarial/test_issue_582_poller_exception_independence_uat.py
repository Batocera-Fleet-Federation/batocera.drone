"""UAT for #582: a tick exception must not retire the shared VPN/RGS poller.

Issue #582: run_self_heal_poller had no durable guard around its tick work, so
one unexpected exception stopped both VPN self-heal and RGS tunnel reconcile.
Each operation is an independent concern that shares a cadence; a transient
failure of one must not starve the other or kill the forever-loop.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class _BlankError(Exception):
    def __str__(self) -> str:
        return ""


class Issue582PollerExceptionIndependenceUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-582-poller-exception",
        }, clear=True):
            self.settings = Settings.from_env()

    def _run_live_ticks(self, *, live_ticks: int, check, reconcile):
        sleeps: list[float] = []
        captured_generations: list[int] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(generation: int) -> bool:
            captured_generations.append(generation)
            return len(sleeps) > live_ticks

        stderr = io.StringIO()
        with mock.patch.object(vpn_manager, "poller_generation", return_value=17), \
                mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_and_self_heal", check), \
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", reconcile), \
                mock.patch.object(vpn_manager.sys, "stderr", stderr):
            vpn_manager.run_self_heal_poller(self.settings)
        return sleeps, stderr, captured_generations

    def test_self_heal_exception_still_reconciles_same_tick_and_later_ticks(self) -> None:
        check = mock.Mock(side_effect=[
            RuntimeError("unexpected self-heal"),
            None,
            TypeError("malformed status payload"),
        ])
        reconcile = mock.Mock(return_value=None)

        sleeps, stderr, generations = self._run_live_ticks(
            live_ticks=3, check=check, reconcile=reconcile,
        )

        self.assertEqual(check.call_count, 3)
        self.assertEqual(reconcile.call_count, 3)
        self.assertEqual(check.call_args_list, [mock.call(self.settings)] * 3)
        self.assertEqual(reconcile.call_args_list, [mock.call(self.settings)] * 3)
        self.assertEqual(len(sleeps), 4)
        self.assertTrue(all(
            interval == vpn_manager.VPN_SELF_HEAL_CHECK_INTERVAL_SECONDS
            for interval in sleeps
        ))
        self.assertTrue(generations)
        self.assertTrue(all(generation == 17 for generation in generations))
        log = stderr.getvalue()
        self.assertIn("VPN self-heal tick failed: RuntimeError: unexpected self-heal", log)
        self.assertIn("VPN self-heal tick failed: TypeError: malformed status payload", log)

    def test_rgs_exception_does_not_stop_later_self_heal_ticks(self) -> None:
        check = mock.Mock(return_value=None)
        reconcile = mock.Mock(side_effect=[
            OSError("RGS restart failed"),
            None,
            ValueError("bad rgs snapshot"),
        ])

        sleeps, stderr, _ = self._run_live_ticks(
            live_ticks=3, check=check, reconcile=reconcile,
        )

        self.assertEqual(check.call_count, 3)
        self.assertEqual(reconcile.call_count, 3)
        self.assertEqual(len(sleeps), 4)
        log = stderr.getvalue()
        self.assertIn("RGS reconcile tick failed: OSError: RGS restart failed", log)
        self.assertIn("RGS reconcile tick failed: ValueError: bad rgs snapshot", log)

    def test_both_operations_raising_on_one_tick_does_not_retire_the_poller(self) -> None:
        check = mock.Mock(side_effect=[
            RuntimeError("heal boom"),
            None,
        ])
        reconcile = mock.Mock(side_effect=[
            OSError("rgs boom"),
            None,
        ])

        _, stderr, _ = self._run_live_ticks(
            live_ticks=2, check=check, reconcile=reconcile,
        )

        self.assertEqual(check.call_count, 2)
        self.assertEqual(reconcile.call_count, 2)
        log = stderr.getvalue()
        self.assertIn("VPN self-heal tick failed: RuntimeError: heal boom", log)
        self.assertIn("RGS reconcile tick failed: OSError: rgs boom", log)

    def test_blank_exception_message_still_logs_the_exception_class(self) -> None:
        check = mock.Mock(side_effect=_BlankError())
        reconcile = mock.Mock(return_value=None)

        _, stderr, _ = self._run_live_ticks(
            live_ticks=1, check=check, reconcile=reconcile,
        )

        self.assertEqual(reconcile.call_count, 1)
        self.assertIn("VPN self-heal tick failed: _BlankError:", stderr.getvalue())

    def test_keyboard_interrupt_is_not_swallowed_by_the_tick_guard(self) -> None:
        check = mock.Mock(side_effect=KeyboardInterrupt)
        reconcile = mock.Mock(return_value=None)

        with self.assertRaises(KeyboardInterrupt):
            self._run_live_ticks(live_ticks=2, check=check, reconcile=reconcile)

        self.assertEqual(check.call_count, 1)
        reconcile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
