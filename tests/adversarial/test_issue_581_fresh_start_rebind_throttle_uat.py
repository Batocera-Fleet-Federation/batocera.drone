"""Adversarial boundary between boot starts and the rebind throttle (issue #581, fresh tester).

Issue Proposed item 2 rate-limits *restarts* so a flapping VPN does not churn
RGS. Starting RGS that was not running is recovery (item 3), not churn. So a
start must not arm the restart throttle: if the tunnel address changes right
after RGS was started on the old address, RGS is still bound to the stale
address and must rebind on the next tick, not 600 s later. The activity log
must also name the restart and its reason (item 4).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class RgsStartRebindThrottleUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-581-start-throttle",
        }, clear=True):
            self.settings = Settings.from_env()
        vpn_manager._save_state(self.settings, has_config=True)

    def _tick(self, *, address: str, running: bool, enabled: bool = True):
        service = {"available": True, "enabled": enabled, "running": running}
        up = dict(service, running=True)
        with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "tunnel_is_up", return_value=True), \
                mock.patch.object(vpn_manager, "_tunnel_ip", return_value=address), \
                mock.patch.object(vpn_manager._rgs_manager, "status", return_value=service), \
                mock.patch.object(vpn_manager._rgs_manager, "start", return_value=up) as start, \
                mock.patch.object(vpn_manager._rgs_manager, "restart", return_value=up) as restart, \
                mock.patch.object(vpn_manager, "_drone_log") as log:
            result = vpn_manager.reconcile_rgs_with_tunnel(self.settings)
        return result, start, restart, log

    def test_address_change_right_after_boot_start_rebinds_without_waiting_out_throttle(self) -> None:
        # Tick 1: VPN up on .17, RGS stopped -> started on .17 (recovery).
        result, start, restart, _ = self._tick(address="10.96.0.17", running=False)
        self.assertEqual(result and result.get("action"), "start")
        start.assert_called_once()
        # Tick 2, seconds later: tunnel flapped to .18 while RGS is bound to .17.
        result, start, restart, _ = self._tick(address="10.96.0.18", running=True)
        self.assertEqual(result and result.get("action"), "restart")
        restart.assert_called_once()

    def test_restart_logs_the_action_and_both_addresses(self) -> None:
        vpn_manager._save_state(self.settings, rgs_tunnel_ip="10.96.0.17")
        result, _, restart, log = self._tick(address="10.96.0.18", running=True)
        self.assertEqual(result and result.get("action"), "restart")
        restart.assert_called_once()
        messages = [call.args[0] for call in log.call_args_list]
        self.assertTrue(
            any("restart" in m and "10.96.0.17" in m and "10.96.0.18" in m for m in messages),
            messages,
        )


if __name__ == "__main__":
    unittest.main()
