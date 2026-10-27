"""Issue #615 boundary UAT: generation zero is a valid captured snapshot."""

from __future__ import annotations

import unittest
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.common import runtime_state


class ExplicitZeroGenerationUAT(unittest.TestCase):
    def test_zero_snapshot_is_not_treated_as_missing_and_recaptured(self) -> None:
        # Retirement has advanced the live generation above zero. The supplied
        # generation is nevertheless an explicit, stale caller snapshot.
        runtime_state.retire_pollers()
        self.assertNotEqual(runtime_state.poller_generation(), 0)
        check = mock.Mock(return_value=False)
        sleep = mock.Mock()

        with mock.patch.object(vpn_manager, "check_sharing_revocation", check), \
                mock.patch.object(vpn_manager.time, "sleep", new=sleep), \
                mock.patch.object(
                    vpn_manager,
                    "poller_generation",
                    side_effect=AssertionError("explicit generation 0 was recaptured as missing"),
                ):
            vpn_manager.run_sharing_revocation_poller(mock.sentinel.settings, generation=0)

        self.assertEqual(sleep.call_count, 1, "retired poller should wake once to observe retirement")
        self.assertEqual(check.call_count, 0, "stale generation 0 must not run a check")


if __name__ == "__main__":
    unittest.main()
