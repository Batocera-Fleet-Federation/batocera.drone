"""A self-heal failure must not let a retired generation act on RGS."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from app.common import runtime_state
import app.device.vpn_manager as vpn_manager


class _StopPoller(Exception):
    pass


def _utf8_stderr() -> io.TextIOWrapper:
    return io.TextIOWrapper(
        io.BytesIO(), encoding="utf-8", errors="strict", write_through=True,
    )


class RetiredFailedCheckSecurityTests(unittest.TestCase):
    def test_retirement_during_failed_check_skips_rgs_reconcile(self) -> None:
        generation = runtime_state.poller_generation()
        reconcile = mock.Mock()
        sleep_calls = 0

        def failed_check(_settings: object) -> tuple[None, bool]:
            runtime_state.retire_pollers()
            return None, True

        def sleep(_seconds: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise _StopPoller()

        with mock.patch.object(vpn_manager, "poller_generation", return_value=generation), \
                mock.patch.object(vpn_manager, "poller_retired", side_effect=lambda _g: runtime_state.poller_generation() != generation), \
                mock.patch.object(vpn_manager, "_check_and_self_heal", side_effect=failed_check), \
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", reconcile), \
                mock.patch.object(vpn_manager.time, "sleep", side_effect=sleep):
            vpn_manager.run_self_heal_poller(object())

        self.assertEqual(reconcile.call_count, 0)

    def test_real_swallowed_inner_failure_during_retirement_skips_rgs(self) -> None:
        """Retirement during a real check body that catches and logs must still skip RGS.

        UAT that requires reconcile after this path is asserting an unsafe contract.
        Conditioning the gate on the unused failed flag (``if not check_failed``)
        would start rgs_download after retire_pollers() during shutdown.
        """
        generation = runtime_state.poller_generation()
        reconcile = mock.Mock()
        sleep_calls = 0

        def load_state_retires_and_fails(_settings: object):
            runtime_state.retire_pollers()
            raise RuntimeError("openvpn log: bad \udcff path")

        def sleep(_seconds: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise _StopPoller()

        with mock.patch.object(vpn_manager, "poller_generation", return_value=generation), \
                mock.patch.object(vpn_manager, "poller_retired", side_effect=lambda _g: runtime_state.poller_generation() != generation), \
                mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "_load_state", side_effect=load_state_retires_and_fails), \
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", reconcile), \
                mock.patch.object(vpn_manager.time, "sleep", side_effect=sleep), \
                mock.patch.object(vpn_manager.sys, "stderr", _utf8_stderr()):
            try:
                vpn_manager.run_self_heal_poller(object())
            except _StopPoller:
                self.fail("retired poller kept ticking instead of returning")

        self.assertEqual(
            reconcile.call_count,
            0,
            "a generation retired during a swallowed self-heal failure must not start or restart RGS",
        )


if __name__ == "__main__":
    unittest.main()
