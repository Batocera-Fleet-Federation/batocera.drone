"""A retired VPN/RGS poller must not perform an RGS action after shutdown retired it.

The shutdown gate in run_self_heal_poller exists so a stale generation never runs
more work (RGS restart/start plus a state-database write) once retire_pollers()
has been called. A failed self-heal tick must not bypass that gate.
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.common import runtime_state
import app.device.vpn_manager as vpn_manager


class _StopPoller(Exception):
    """Raised by the patched sleep so a buggy poller cannot loop forever."""


class RetiredPollerRgsActionTests(unittest.TestCase):
    def test_failed_self_heal_then_retirement_performs_no_rgs_action(self) -> None:
        reconcile_calls: list[object] = []
        sleep_calls: list[float] = []

        def retiring_self_heal(_settings: object) -> None:
            # Shutdown retires the poller while this tick's slow check is running.
            runtime_state.retire_pollers()
            raise RuntimeError("self-heal failed mid-shutdown")

        def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            if len(sleep_calls) > 1:
                raise _StopPoller()

        def record_reconcile(settings: object) -> None:
            reconcile_calls.append(settings)

        with mock.patch.object(vpn_manager, "check_and_self_heal", retiring_self_heal), \
             mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", record_reconcile), \
             mock.patch.object(vpn_manager, "_log_poller_failure", lambda *_args: None), \
             mock.patch.object(vpn_manager.time, "sleep", fake_sleep):
            try:
                vpn_manager.run_self_heal_poller(object())
            except _StopPoller:
                self.fail("retired poller kept ticking instead of returning")

        self.assertEqual(
            reconcile_calls,
            [],
            "a poller retired during a failed self-heal tick must not reconcile RGS",
        )


if __name__ == "__main__":
    unittest.main()
