"""Gate and logging UAT for VPN-triggered RGS recovery (#581)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class RgsReconcileGatesUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-581-gates",
        }, clear=True):
            self.settings = Settings.from_env()
        vpn_manager._save_state(self.settings, has_config=True, rgs_tunnel_ip="10.96.0.17")

    def run_reconcile(self, *, allowed=True, up=True, address="10.96.0.18", available=True):
        service = {"available": available, "enabled": True, "running": True}
        with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=allowed), \
                mock.patch.object(vpn_manager, "tunnel_is_up", return_value=up), \
                mock.patch.object(vpn_manager, "_tunnel_ip", return_value=address), \
                mock.patch.object(vpn_manager._rgs_manager, "status", return_value=service) as status, \
                mock.patch.object(vpn_manager._rgs_manager, "restart", return_value=service) as restart, \
                mock.patch.object(vpn_manager._rgs_manager, "start") as start, \
                mock.patch.object(vpn_manager, "_drone_log") as log:
            result = vpn_manager.reconcile_rgs_with_tunnel(self.settings)
        return result, status, restart, start, log

    def test_identity_hold_prevents_service_inspection_and_action(self) -> None:
        result, status, restart, start, _ = self.run_reconcile(allowed=False)
        self.assertIsNone(result)
        status.assert_not_called()
        restart.assert_not_called()
        start.assert_not_called()

    def test_down_or_unreadable_tunnel_leaves_last_address_unchanged(self) -> None:
        for options in ({"up": False}, {"address": None}, {"address": ""}):
            with self.subTest(options=options):
                result, status, restart, start, _ = self.run_reconcile(**options)
                self.assertIsNone(result)
                status.assert_not_called()
                restart.assert_not_called()
                start.assert_not_called()
                self.assertEqual(vpn_manager._load_state(self.settings)["rgs_tunnel_ip"], "10.96.0.17")

    def test_unavailable_rgs_is_retried_on_later_poll_without_marking_address(self) -> None:
        result, _, restart, start, _ = self.run_reconcile(available=False)
        self.assertIsNone(result)
        restart.assert_not_called()
        start.assert_not_called()
        self.assertEqual(vpn_manager._load_state(self.settings)["rgs_tunnel_ip"], "10.96.0.17")

        result, _, restart, _, log = self.run_reconcile()
        self.assertEqual(result and result.get("action"), "restart")
        restart.assert_called_once_with(self.settings)
        self.assertEqual(vpn_manager._load_state(self.settings)["rgs_tunnel_ip"], "10.96.0.18")
        log.assert_called_once()
        self.assertIn("10.96.0.17 to 10.96.0.18", log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
