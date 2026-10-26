"""Adversarial acceptance boundaries for RGS tunnel reconciliation (#581)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class RgsTunnelReconcileEdgesUAT(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        env = {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-581",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        vpn_manager._save_state(self.settings, has_config=True)

    def reconcile(self, address: str, rgs: dict):
        with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "tunnel_is_up", return_value=True), \
                mock.patch.object(vpn_manager, "_tunnel_ip", return_value=address), \
                mock.patch.object(vpn_manager._rgs_manager, "status", return_value=rgs), \
                mock.patch.object(vpn_manager, "_drone_log"):
            return vpn_manager.reconcile_rgs_with_tunnel(self.settings)

    def test_enabling_stopped_rgs_after_disabled_observation_starts_on_same_tunnel(self) -> None:
        """Disabled must not start, but its address observation cannot mask later boot recovery."""
        disabled = {"available": True, "enabled": False, "running": False}
        with mock.patch.object(vpn_manager._rgs_manager, "start") as start, \
                mock.patch.object(vpn_manager._rgs_manager, "restart") as restart:
            self.assertIsNone(self.reconcile("10.96.0.17", disabled))
            start.assert_not_called()
            restart.assert_not_called()

            enabled_stopped = {"available": True, "enabled": True, "running": False}
            result = self.reconcile("10.96.0.17", enabled_stopped)

        self.assertEqual(result and result.get("action"), "start")
        start.assert_called_once_with(self.settings)
        restart.assert_not_called()

    def test_non_string_rate_limit_timestamp_does_not_silently_suppress_rebind(self) -> None:
        """Malformed persisted bookkeeping must not prevent the in-scope rebind action."""
        vpn_manager._save_state(
            self.settings,
            rgs_tunnel_ip="10.96.0.17",
            rgs_restart_last_at={"unexpected": "object"},
        )
        rgs = {"available": True, "enabled": True, "running": True}
        with mock.patch.object(vpn_manager._rgs_manager, "status", return_value=rgs), \
                mock.patch.object(vpn_manager._rgs_manager, "restart", return_value=rgs) as restart, \
                mock.patch.object(vpn_manager._rgs_manager, "start") as start, \
                mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "tunnel_is_up", return_value=True), \
                mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.96.0.18"), \
                mock.patch.object(vpn_manager, "_drone_log"):
            result = vpn_manager.reconcile_rgs_with_tunnel(self.settings)

        self.assertEqual(result and result.get("action"), "restart")
        restart.assert_called_once_with(self.settings)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
