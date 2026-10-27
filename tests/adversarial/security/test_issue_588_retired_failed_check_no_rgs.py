"""A self-heal failure must not let a retired generation act on RGS."""

from __future__ import annotations

import unittest
from unittest import mock

from app.common import runtime_state
import app.device.vpn_manager as vpn_manager


class _StopPoller(Exception):
    pass


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


if __name__ == "__main__":
    unittest.main()
