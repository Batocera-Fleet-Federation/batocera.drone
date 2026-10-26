"""Issue #582: reporting a tick exception must not retire the shared poller.

The original defect is that one unexpected exception from check_and_self_heal
kills run_self_heal_poller, so reconcile_rgs_with_tunnel never runs again.
A guard that re-raises while formatting or writing the log has the same
outcome on a real stderr (TextIOWrapper, strict UTF-8): the thread dies and
RGS recovery stops.

Production paths that surface this: os.fsdecode surrogate-escaped paths in
exception text, and a broken/closed stderr pipe (journald/EIO).
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class _ClosedStderr(io.TextIOBase):
    encoding = "utf-8"
    errors = "strict"

    def write(self, _data: str) -> int:
        raise OSError("EIO")

    def flush(self) -> None:
        raise OSError("EIO")


def _utf8_stderr() -> io.TextIOWrapper:
    return io.TextIOWrapper(
        io.BytesIO(),
        encoding="utf-8",
        errors="strict",
        newline="\n",
        write_through=True,
    )


class Issue582PollerLogIsolationUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-582-log-isolation",
        }, clear=True):
            self.settings = Settings.from_env()

    def _run(self, *, live_ticks: int, check, reconcile, stderr):
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(_generation: int) -> bool:
            return len(sleeps) > live_ticks

        caught: Optional[Exception] = None
        with mock.patch.object(vpn_manager, "poller_generation", return_value=82), \
                mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_and_self_heal", check), \
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", reconcile), \
                mock.patch.object(vpn_manager.sys, "stderr", stderr):
            try:
                vpn_manager.run_self_heal_poller(self.settings)
            except Exception as error:
                caught = error
        return sleeps, caught

    def test_surrogate_self_heal_message_on_utf8_stderr_still_reconciles(self) -> None:
        # os.fsdecode leaves lone surrogates in exception text; UTF-8 stderr
        # cannot encode them, so a naive print() in the guard kills the loop.
        check = mock.Mock(side_effect=[
            RuntimeError("openvpn log: bad \udcff path"),
            None,
        ])
        reconcile = mock.Mock(return_value=None)

        sleeps, caught = self._run(
            live_ticks=2, check=check, reconcile=reconcile, stderr=_utf8_stderr(),
        )

        self.assertIsNone(
            caught,
            f"poller died while logging a surrogate self-heal error: {caught!r}",
        )
        self.assertEqual(check.call_count, 2)
        self.assertEqual(reconcile.call_count, 2)
        self.assertEqual(reconcile.call_args_list, [mock.call(self.settings)] * 2)
        self.assertEqual(len(sleeps), 3)
        self.assertTrue(all(
            interval == vpn_manager.VPN_SELF_HEAL_CHECK_INTERVAL_SECONDS
            for interval in sleeps
        ))

    def test_surrogate_rgs_message_on_utf8_stderr_still_self_heals(self) -> None:
        check = mock.Mock(return_value=None)
        reconcile = mock.Mock(side_effect=[
            OSError("rgs unit: bad \udcff path"),
            None,
        ])

        _, caught = self._run(
            live_ticks=2, check=check, reconcile=reconcile, stderr=_utf8_stderr(),
        )

        self.assertIsNone(
            caught,
            f"poller died while logging a surrogate RGS error: {caught!r}",
        )
        self.assertEqual(check.call_count, 2)
        self.assertEqual(reconcile.call_count, 2)

    def test_closed_stderr_during_self_heal_log_does_not_starve_rgs(self) -> None:
        check = mock.Mock(side_effect=[
            RuntimeError("unexpected self-heal"),
            None,
        ])
        reconcile = mock.Mock(return_value=None)

        _, caught = self._run(
            live_ticks=2, check=check, reconcile=reconcile, stderr=_ClosedStderr(),
        )

        self.assertIsNone(
            caught,
            f"poller died because stderr write failed while logging self-heal: {caught!r}",
        )
        self.assertEqual(check.call_count, 2)
        self.assertEqual(reconcile.call_count, 2)

    def test_closed_stderr_during_rgs_log_does_not_stop_later_self_heal(self) -> None:
        check = mock.Mock(return_value=None)
        reconcile = mock.Mock(side_effect=[
            OSError("RGS restart failed"),
            None,
        ])

        _, caught = self._run(
            live_ticks=2, check=check, reconcile=reconcile, stderr=_ClosedStderr(),
        )

        self.assertIsNone(
            caught,
            f"poller died because stderr write failed while logging RGS: {caught!r}",
        )
        self.assertEqual(check.call_count, 2)
        self.assertEqual(reconcile.call_count, 2)


if __name__ == "__main__":
    unittest.main()
