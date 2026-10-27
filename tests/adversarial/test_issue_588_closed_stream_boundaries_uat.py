"""Issue #588: closed, ASCII-only and missing stdio must not stop VPN/RGS recovery.

#582 requires the shared VPN/RGS poller to survive a failed log write, and a
failed watchdog tick must not starve the RGS reconcile on that same tick. These
probes cover stdio shapes the earlier suites did not exercise:

* ASCII-only stderr (LANG=C style) receiving valid non-ASCII text
* sys.stderr set to None (detached daemon)
* a long alternating run of surrogate and closed-stream failures
* a successful RGS restart whose log write fails must still be recorded, so a
  closed stdout cannot cause a restart on every later tick
* a failed RGS restart whose log write fails must still honour the restart
  throttle on the next tick
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.common.logging_setup as logging_setup
import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings

SAMPLE_OVPN = (
    "client\ndev tun\nproto udp\nremote vpn.example.net 1194\n"
    "auth-user-pass\n"
)


class _ClosedStream(io.TextIOBase):
    encoding = "utf-8"
    errors = "strict"

    def write(self, _text: str) -> int:
        raise ValueError("I/O operation on closed file.")

    def flush(self) -> None:
        raise ValueError("I/O operation on closed file.")


def _ascii_stderr() -> io.TextIOWrapper:
    return io.TextIOWrapper(
        io.BytesIO(), encoding="ascii", errors="strict", write_through=True,
    )


def _utf8_sink() -> io.TextIOWrapper:
    return io.TextIOWrapper(
        io.BytesIO(), encoding="utf-8", errors="strict", write_through=True,
    )


class Issue588ClosedStreamBoundariesUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        install = mock.patch.object(
            vpn_manager, "_drone_install_root", return_value=root / "install-root",
        )
        self.addCleanup(install.stop)
        install.start()
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-588-closed-stream",
        }, clear=True):
            self.settings = Settings.from_env()

    def _run_poller(self, *, live_ticks: int, check, reconcile, stderr, stdout=None):
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(_generation: int) -> bool:
            return len(sleeps) > live_ticks

        caught: Exception | None = None
        with mock.patch.object(vpn_manager, "poller_generation", return_value=588), \
                mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_and_self_heal", check), \
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", reconcile), \
                mock.patch.object(vpn_manager.sys, "stdout", stdout or _utf8_sink()), \
                mock.patch.object(vpn_manager.sys, "stderr", stderr):
            try:
                vpn_manager.run_self_heal_poller(self.settings)
            except Exception as error:
                caught = error
        return sleeps, caught

    def test_ascii_stderr_with_non_ascii_failure_text_keeps_both_helpers_running(self) -> None:
        # Valid UTF-8 text is not encodable on an ASCII stream; the guard must
        # absorb that instead of letting it stop the poller.
        check = mock.Mock(side_effect=[
            RuntimeError("openvpn: café unreachable \udcff"),
            None,
        ])
        reconcile = mock.Mock(side_effect=[
            OSError("rgs unit: naïve path"),
            None,
        ])

        sleeps, caught = self._run_poller(
            live_ticks=2, check=check, reconcile=reconcile, stderr=_ascii_stderr(),
        )

        self.assertIsNone(caught, f"poller died on ASCII stderr: {caught!r}")
        self.assertEqual(check.call_count, 2)
        self.assertEqual(reconcile.call_count, 2)
        self.assertEqual(len(sleeps), 3)

    def test_missing_stderr_does_not_stop_the_poller(self) -> None:
        check = mock.Mock(side_effect=RuntimeError("openvpn log: bad \udcff path"))
        reconcile = mock.Mock(side_effect=OSError("rgs unit: bad \udcff path"))

        _, caught = self._run_poller(
            live_ticks=3, check=check, reconcile=reconcile, stderr=None,
        )

        self.assertIsNone(caught, f"poller died with sys.stderr=None: {caught!r}")
        self.assertEqual(check.call_count, 3)
        self.assertEqual(reconcile.call_count, 3)

    def test_long_alternating_failure_streak_reconciles_every_live_tick(self) -> None:
        # Checks fail on even ticks and RGS fails on odd ticks, with stderr
        # closed the whole time. Every live tick must still run both helpers.
        live_ticks = 25
        check_outcomes = [
            RuntimeError("streak check \udcff") if tick % 2 == 0 else None
            for tick in range(live_ticks)
        ]
        reconcile_outcomes = [
            OSError("streak rgs \udcff") if tick % 2 == 1 else None
            for tick in range(live_ticks)
        ]
        check = mock.Mock(side_effect=check_outcomes)
        reconcile = mock.Mock(side_effect=reconcile_outcomes)

        sleeps, caught = self._run_poller(
            live_ticks=live_ticks, check=check, reconcile=reconcile, stderr=_ClosedStream(),
        )

        self.assertIsNone(caught, f"poller died during the streak: {caught!r}")
        self.assertEqual(check.call_count, live_ticks)
        self.assertEqual(reconcile.call_count, live_ticks)
        self.assertEqual(len(sleeps), live_ticks + 1)

    def test_restart_with_closed_stdout_is_recorded_and_not_repeated(self) -> None:
        # The tunnel-address record is written before the summary log line, so a
        # closed stdout must not make the next tick restart RGS again.
        self._save_tunnel_config()
        vpn_manager._save_state(self.settings, rgs_tunnel_ip="10.8.0.4")

        first, restart = self._reconcile_rgs(stdout=_ClosedStream())
        second, _ = self._reconcile_rgs(stdout=_utf8_sink())

        self.assertEqual(restart.call_count, 1, "a closed stdout must not cause repeated RGS restarts")
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(vpn_manager._load_state(self.settings)["rgs_tunnel_ip"], "10.8.0.5")

    def test_failed_restart_with_closed_stdout_still_honours_the_throttle(self) -> None:
        self._save_tunnel_config()
        vpn_manager._save_state(self.settings, rgs_tunnel_ip="10.8.0.4")

        _, restart = self._reconcile_rgs(
            stdout=_ClosedStream(), restart_error=OSError("rgs unit: bad \udcff path"),
        )
        second, _ = self._reconcile_rgs(stdout=_utf8_sink())

        self.assertEqual(restart.call_count, 1, "a failed restart must still arm the throttle")
        self.assertEqual(second, {"action": "deferred", "reason": mock.ANY})
        self.assertEqual(vpn_manager._load_state(self.settings)["rgs_tunnel_ip"], "10.8.0.4")

    def _save_tunnel_config(self) -> None:
        vpn_manager.save_uploaded_config(self.settings, "client.ovpn", SAMPLE_OVPN.encode())

    def _reconcile_rgs(self, *, stdout, restart_error: BaseException | None = None):
        restart = mock.Mock(side_effect=restart_error)
        with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "tunnel_is_up", return_value=True), \
                mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.8.0.5"), \
                mock.patch.object(vpn_manager._rgs_manager, "status", return_value={
                    "available": True, "enabled": True, "running": True,
                }), \
                mock.patch.object(vpn_manager._rgs_manager, "restart", restart), \
                mock.patch.object(logging_setup, "_DRONE_ACTIVITY_LOG_STREAM", None), \
                mock.patch.object(vpn_manager.sys, "stdout", stdout), \
                mock.patch.object(vpn_manager.sys, "stderr", _utf8_sink()):
            result = vpn_manager.reconcile_rgs_with_tunnel(self.settings)
        return result, restart


if __name__ == "__main__":
    unittest.main()
