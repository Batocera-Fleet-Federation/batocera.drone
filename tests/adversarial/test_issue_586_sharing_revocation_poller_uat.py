"""UAT for #586: a tick exception must not retire the VPN sharing-revocation poller.

Issue #586: run_sharing_revocation_poller calls check_sharing_revocation with no
tick-level guard, so one unexpected exception ends the forever-loop and no later
revocation is ever checked. Expected behavior: a failing tick is logged, the
poller keeps its cadence and runs the next check, and it still returns promptly
once its generation is retired. Only Exception is absorbed; a BaseException such
as KeyboardInterrupt is a shutdown signal and must propagate.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.common import runtime_state
from app.drone_api import Settings


SAMPLE_OVPN = """client
dev tun
proto udp
remote vpn.example.net 1194
remote-cert-tls server
auth-user-pass
up /etc/openvpn/update-resolv-conf
down /etc/openvpn/update-resolv-conf
verb 3
"""


class _BlankError(Exception):
    def __str__(self) -> str:
        return ""


class _UnprintableError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("cannot format")


class _ClosedStream:
    """Stands in for a closed stderr: every write and flush fails."""

    def write(self, _text: str) -> int:
        raise OSError("stderr closed")

    def flush(self) -> None:
        raise OSError("stderr closed")


class Issue586SharingRevocationPollerUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        # vpn_dir() is not a Settings field; pin it under the temp root so an
        # import_from_peer call in the integration test never touches a real install.
        patcher = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install-root")
        self.addCleanup(patcher.stop)
        patcher.start()
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-586-sharing-revocation-poller",
        }, clear=True):
            self.settings = Settings.from_env()

    def _run_live_ticks(self, *, live_ticks: int, check, stderr=None):
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(_generation: int) -> bool:
            # Stay live for ``live_ticks`` wake-ups, then report retirement.
            return len(sleeps) > live_ticks

        stream = stderr if stderr is not None else io.StringIO()
        with mock.patch.object(vpn_manager, "poller_generation", return_value=23), \
                mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", check), \
                mock.patch.object(vpn_manager.sys, "stderr", stream):
            vpn_manager.run_sharing_revocation_poller(self.settings)
        return sleeps, stream

    def test_unexpected_exception_does_not_stop_later_ticks(self) -> None:
        check = mock.Mock(side_effect=[RuntimeError("unexpected"), False, False])

        sleeps, _ = self._run_live_ticks(live_ticks=3, check=check)

        self.assertEqual(check.call_count, 3)
        self.assertEqual(check.call_args_list, [mock.call(self.settings)] * 3)
        self.assertEqual(len(sleeps), 4)

    def test_repeated_consecutive_failures_keep_the_poller_alive(self) -> None:
        check = mock.Mock(side_effect=[RuntimeError("unexpected")] * 5 + [False])

        _, stderr = self._run_live_ticks(live_ticks=6, check=check)

        self.assertEqual(check.call_count, 6)
        self.assertEqual(stderr.getvalue().count("VPN sharing revocation check failed"), 5)

    def test_failure_is_logged_with_class_and_message(self) -> None:
        check = mock.Mock(side_effect=[RuntimeError("unexpected"), False])

        _, stderr = self._run_live_ticks(live_ticks=2, check=check)

        self.assertIn("VPN sharing revocation check failed: RuntimeError: unexpected", stderr.getvalue())

    def test_blank_message_failure_is_still_logged_and_survived(self) -> None:
        check = mock.Mock(side_effect=[_BlankError(), False])

        _, stderr = self._run_live_ticks(live_ticks=2, check=check)

        self.assertEqual(check.call_count, 2)
        self.assertIn("VPN sharing revocation check failed: _BlankError:", stderr.getvalue())

    def test_unprintable_exception_does_not_stop_the_poller(self) -> None:
        check = mock.Mock(side_effect=[_UnprintableError(), False])

        _, stderr = self._run_live_ticks(live_ticks=2, check=check)

        self.assertEqual(check.call_count, 2)
        self.assertIn("_UnprintableError: <unprintable exception>", stderr.getvalue())

    def test_closed_stderr_does_not_stop_the_poller(self) -> None:
        check = mock.Mock(side_effect=[RuntimeError("unexpected"), False])

        sleeps, _ = self._run_live_ticks(live_ticks=2, check=check, stderr=_ClosedStream())

        self.assertEqual(check.call_count, 2)
        self.assertEqual(len(sleeps), 3)

    def test_failing_ticks_keep_the_configured_cadence(self) -> None:
        check = mock.Mock(side_effect=[RuntimeError("a"), RuntimeError("b"), False])

        with mock.patch.dict("os.environ", {"DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS": "120"}):
            sleeps, _ = self._run_live_ticks(live_ticks=3, check=check)

        # One sleep before every tick, failed or not: no hot loop after a failure.
        self.assertEqual(sleeps, [120.0] * 4)

    def test_already_retired_poller_makes_no_check(self) -> None:
        check = mock.Mock(side_effect=AssertionError("must not check after retirement"))

        sleeps, _ = self._run_live_ticks(live_ticks=0, check=check)

        self.assertEqual(len(sleeps), 1)
        check.assert_not_called()

    def test_keyboard_interrupt_is_not_swallowed_as_a_tick_failure(self) -> None:
        check = mock.Mock(side_effect=KeyboardInterrupt())

        with self.assertRaises(KeyboardInterrupt):
            self._run_live_ticks(live_ticks=3, check=check)

        self.assertEqual(check.call_count, 1)

    def test_retirement_during_a_failing_tick_stops_before_the_next_check(self) -> None:
        # Real generation gate (no poller_* patches): shutdown retires the poller
        # while the failing tick is still running, so the next wake must return.
        calls: list[Settings] = []

        def check_then_retire(settings: Settings) -> bool:
            calls.append(settings)
            runtime_state.retire_pollers()
            raise RuntimeError("unexpected during shutdown")

        with mock.patch.object(vpn_manager, "check_sharing_revocation", side_effect=check_then_retire), \
                mock.patch.object(vpn_manager.time, "sleep"), \
                mock.patch.object(vpn_manager.sys, "stderr", io.StringIO()):
            vpn_manager.run_sharing_revocation_poller(self.settings)

        self.assertEqual(len(calls), 1)

    def test_real_revocation_still_applies_after_an_earlier_tick_failure(self) -> None:
        payload = {
            "config_filename": "client.ovpn",
            "config_text": SAMPLE_OVPN,
            "has_credentials": True,
            "username": "peeruser",
            "password": "peerpass123",
        }
        vpn_manager.import_from_peer(self.settings, payload, source_peer_id="peer-1", source_peer_name="Peer One")
        real_check = vpn_manager.check_sharing_revocation
        calls: list[int] = []

        def first_tick_fails_then_real_check(settings: Settings) -> bool:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("unexpected first tick")
            return real_check(settings)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            _, stderr = self._run_live_ticks(live_ticks=2, check=first_tick_fails_then_real_check)

        self.assertEqual(len(calls), 2)
        self.assertIn("VPN sharing revocation check failed: RuntimeError: unexpected first tick", stderr.getvalue())
        disconnect.assert_called()
        state = vpn_manager._load_state(self.settings)
        self.assertFalse(state["has_credentials"])
        self.assertIn("no longer paired", state["revoked_reason"])


if __name__ == "__main__":
    unittest.main()
