"""Boundary UAT for #582's shared VPN self-heal / RGS poller guard.

Issue #582 requires the forever-loop to survive unexpected Exception from
either tick operation. BaseException control-flow (SystemExit, KeyboardInterrupt)
must still propagate. Logging a well-formed unicode message on a real UTF-8
stderr must not be mistaken for the surrogate-encoding failure case.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class _NonStrError(Exception):
    def __str__(self):  # type: ignore[override]
        return None


class _UnprintableError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("exception formatting failed")


def _utf8_stderr() -> io.TextIOWrapper:
    return io.TextIOWrapper(
        io.BytesIO(),
        encoding="utf-8",
        errors="strict",
        newline="\n",
        write_through=True,
    )


class Issue582PollerExceptionBoundariesUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-582-exception-boundaries",
        }, clear=True):
            self.settings = Settings.from_env()

    def _run(self, *, live_ticks: int, check, reconcile, stderr=None):
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(_generation: int) -> bool:
            return len(sleeps) > live_ticks

        if stderr is None:
            stderr = io.StringIO()
        with mock.patch.object(vpn_manager, "poller_generation", return_value=58), \
                mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_and_self_heal", check), \
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", reconcile), \
                mock.patch.object(vpn_manager.sys, "stderr", stderr):
            vpn_manager.run_self_heal_poller(self.settings)
        return sleeps, stderr

    def test_system_exit_from_self_heal_is_not_swallowed(self) -> None:
        check = mock.Mock(side_effect=SystemExit(2))
        reconcile = mock.Mock(return_value=None)

        with self.assertRaises(SystemExit) as raised:
            self._run(live_ticks=3, check=check, reconcile=reconcile)

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(check.call_count, 1)
        reconcile.assert_not_called()

    def test_system_exit_from_rgs_is_not_swallowed(self) -> None:
        check = mock.Mock(return_value=None)
        reconcile = mock.Mock(side_effect=SystemExit(3))

        with self.assertRaises(SystemExit) as raised:
            self._run(live_ticks=3, check=check, reconcile=reconcile)

        self.assertEqual(raised.exception.code, 3)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(reconcile.call_count, 1)

    def test_keyboard_interrupt_from_rgs_is_not_swallowed(self) -> None:
        check = mock.Mock(return_value=None)
        reconcile = mock.Mock(side_effect=KeyboardInterrupt)

        with self.assertRaises(KeyboardInterrupt):
            self._run(live_ticks=3, check=check, reconcile=reconcile)

        self.assertEqual(check.call_count, 1)
        self.assertEqual(reconcile.call_count, 1)

    def test_non_str_exception_message_still_reconciles(self) -> None:
        check = mock.Mock(side_effect=[_NonStrError(), None])
        reconcile = mock.Mock(return_value=None)

        _, stderr = self._run(live_ticks=2, check=check, reconcile=reconcile)

        self.assertEqual(check.call_count, 2)
        self.assertEqual(reconcile.call_count, 2)
        self.assertIn("VPN self-heal tick failed: _NonStrError:", stderr.getvalue())
        self.assertIn("<unprintable exception>", stderr.getvalue())

    def test_unprintable_rgs_exception_does_not_stop_later_self_heal(self) -> None:
        check = mock.Mock(return_value=None)
        reconcile = mock.Mock(side_effect=[_UnprintableError(), None])

        _, stderr = self._run(live_ticks=2, check=check, reconcile=reconcile)

        self.assertEqual(check.call_count, 2)
        self.assertEqual(reconcile.call_count, 2)
        self.assertIn(
            "RGS reconcile tick failed: _UnprintableError: <unprintable exception>",
            stderr.getvalue(),
        )

    def test_valid_unicode_self_heal_message_on_utf8_stderr_continues(self) -> None:
        check = mock.Mock(side_effect=[
            RuntimeError("reconnect failed: 隧道 🔥"),
            None,
        ])
        reconcile = mock.Mock(return_value=None)
        stderr = _utf8_stderr()

        self._run(live_ticks=2, check=check, reconcile=reconcile, stderr=stderr)

        self.assertEqual(check.call_count, 2)
        self.assertEqual(reconcile.call_count, 2)
        logged = stderr.buffer.getvalue().decode("utf-8")
        self.assertIn("VPN self-heal tick failed: RuntimeError: reconnect failed: 隧道 🔥", logged)

    def test_repeated_exceptions_do_not_give_up_the_poller(self) -> None:
        live_ticks = 8
        check = mock.Mock(side_effect=RuntimeError("still broken"))
        reconcile = mock.Mock(side_effect=OSError("rgs still down"))

        sleeps, _ = self._run(live_ticks=live_ticks, check=check, reconcile=reconcile)

        self.assertEqual(check.call_count, live_ticks)
        self.assertEqual(reconcile.call_count, live_ticks)
        self.assertEqual(len(sleeps), live_ticks + 1)


if __name__ == "__main__":
    unittest.main()
