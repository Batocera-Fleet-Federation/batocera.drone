"""Adversarial boot-recovery and persisted-clock boundaries for issue #581."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class RgsRecoveryRateLimitUAT(unittest.TestCase):
    """Exercise the VPN/RGS seam entirely with fake tunnel and service state."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-581-rate-limit",
        }, clear=True):
            self.settings = Settings.from_env()
        vpn_manager._save_state(self.settings, has_config=True)

    def test_boot_start_is_not_deferred_by_a_previous_restart_throttle(self) -> None:
        """Once VPN is up, enabled-but-stopped RGS must recover even after a recent restart."""
        recent = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        vpn_manager._save_state(
            self.settings,
            rgs_tunnel_ip="10.96.0.17",
            rgs_restart_last_at=recent,
        )
        stopped_enabled = {"available": True, "enabled": True, "running": False}
        with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "tunnel_is_up", return_value=True), \
                mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.96.0.18"), \
                mock.patch.object(vpn_manager._rgs_manager, "status", return_value=stopped_enabled), \
                mock.patch.object(vpn_manager._rgs_manager, "start", return_value=stopped_enabled) as start, \
                mock.patch.object(vpn_manager._rgs_manager, "restart") as restart, \
                mock.patch.object(vpn_manager, "_drone_log"):
            result = vpn_manager.reconcile_rgs_with_tunnel(self.settings)

        self.assertEqual(result and result.get("action"), "start")
        start.assert_called_once_with(self.settings)
        restart.assert_not_called()

    def test_future_dated_restart_bookkeeping_does_not_block_a_rebind_forever(self) -> None:
        """Clock rollback/corrupt future state is not a legitimate current rate-limit lease."""
        future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
        vpn_manager._save_state(
            self.settings,
            rgs_tunnel_ip="10.96.0.17",
            rgs_restart_last_at=future,
        )
        running_enabled = {"available": True, "enabled": True, "running": True}
        with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "tunnel_is_up", return_value=True), \
                mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.96.0.18"), \
                mock.patch.object(vpn_manager._rgs_manager, "status", return_value=running_enabled), \
                mock.patch.object(vpn_manager._rgs_manager, "restart", return_value=running_enabled) as restart, \
                mock.patch.object(vpn_manager, "_drone_log"):
            result = vpn_manager.reconcile_rgs_with_tunnel(self.settings)

        self.assertEqual(result and result.get("action"), "restart")
        restart.assert_called_once_with(self.settings)


if __name__ == "__main__":
    unittest.main()
