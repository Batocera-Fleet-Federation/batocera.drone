"""Retirement-gate UAT for #582's shared VPN self-heal / RGS poller.

#581 required: if shutdown retires the poller during a successful slow
check_and_self_heal, do not start the RGS service action.
#582 requires: a failed check must still attempt reconcile on that tick so
RGS cannot be starved by the watchdog exception, but only while the
generation is still live. Retirement during a failed check skips RGS
(security/test_issue_582_retired_poller_rgs_action.py and the VPN skill's
unconditional retirement gate). After either operation, a retired
generation must not start another tick.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class Issue582PollerRetirementAfterFailureUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-582-poller-retirement",
        }, clear=True):
            self.settings = Settings.from_env()

    def _run(self, *, retired, check, reconcile):
        stderr = io.StringIO()
        with mock.patch.object(vpn_manager, "poller_generation", return_value=9), \
                mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                mock.patch.object(vpn_manager.time, "sleep"), \
                mock.patch.object(vpn_manager, "check_and_self_heal", check), \
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", reconcile), \
                mock.patch.object(vpn_manager.sys, "stderr", stderr):
            vpn_manager.run_self_heal_poller(self.settings)
        return stderr

    def test_successful_slow_check_skips_rgs_when_retired_mid_tick(self) -> None:
        retired_now = {"value": False}

        def check(_settings) -> None:
            retired_now["value"] = True

        def retired(_generation: int) -> bool:
            return retired_now["value"]

        check_mock = mock.Mock(side_effect=check)
        reconcile = mock.Mock()
        self._run(retired=retired, check=check_mock, reconcile=reconcile)

        check_mock.assert_called_once_with(self.settings)
        reconcile.assert_not_called()

    def test_failed_check_still_reconciles_while_generation_is_live(self) -> None:
        check_mock = mock.Mock(side_effect=RuntimeError("slow check exploded"))
        reconcile = mock.Mock(return_value=None)

        def retired(_generation: int) -> bool:
            return reconcile.call_count > 0

        stderr = self._run(retired=retired, check=check_mock, reconcile=reconcile)

        check_mock.assert_called_once_with(self.settings)
        reconcile.assert_called_once_with(self.settings)
        self.assertIn(
            "VPN self-heal tick failed: RuntimeError: slow check exploded",
            stderr.getvalue(),
        )

    def test_failed_check_still_reconciles_when_retired_mid_tick(self) -> None:
        # Adjudicated against security/test_issue_582_retired_poller_rgs_action.py:
        # retirement during a failed self-heal must not start RGS. The live-
        # generation case is test_failed_check_still_reconciles_while_generation_is_live.
        retired_now = {"value": False}

        def check(_settings) -> None:
            retired_now["value"] = True
            raise RuntimeError("slow check exploded")

        def retired(_generation: int) -> bool:
            return retired_now["value"]

        check_mock = mock.Mock(side_effect=check)
        reconcile = mock.Mock(return_value=None)
        stderr = self._run(retired=retired, check=check_mock, reconcile=reconcile)

        check_mock.assert_called_once_with(self.settings)
        reconcile.assert_not_called()
        self.assertIn(
            "VPN self-heal tick failed: RuntimeError: slow check exploded",
            stderr.getvalue(),
        )

    def test_retirement_during_rgs_prevents_another_tick(self) -> None:
        retired_now = {"value": False}

        def reconcile(_settings) -> None:
            retired_now["value"] = True

        def retired(_generation: int) -> bool:
            return retired_now["value"]

        check = mock.Mock(return_value=None)
        reconcile_mock = mock.Mock(side_effect=reconcile)
        self._run(retired=retired, check=check, reconcile=reconcile_mock)

        check.assert_called_once_with(self.settings)
        reconcile_mock.assert_called_once_with(self.settings)

    def test_rgs_exception_during_retirement_does_not_start_another_tick(self) -> None:
        retired_now = {"value": False}

        def reconcile(_settings) -> None:
            retired_now["value"] = True
            raise OSError("rgs down during shutdown")

        def retired(_generation: int) -> bool:
            return retired_now["value"]

        check = mock.Mock(return_value=None)
        reconcile_mock = mock.Mock(side_effect=reconcile)
        stderr = self._run(retired=retired, check=check, reconcile=reconcile_mock)

        check.assert_called_once_with(self.settings)
        reconcile_mock.assert_called_once_with(self.settings)
        self.assertIn(
            "RGS reconcile tick failed: OSError: rgs down during shutdown",
            stderr.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
