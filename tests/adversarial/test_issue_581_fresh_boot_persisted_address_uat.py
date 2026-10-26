"""Adversarial reboot-recovery boundary for issue #581 (fresh tester).

Expected behaviour comes from the issue's Proposed item 3: once the VPN is up,
an enabled-but-stopped rgs_download must be started. After a reboot the Drone
still has the pre-reboot tunnel address persisted, and Proton may hand back the
same address, so the recovery start must not depend on the address having
changed. Everything is faked (tunnel, service); no device is needed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class RgsBootPersistedAddressUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-581-boot-persisted",
        }, clear=True):
            self.settings = Settings.from_env()
        # State as persisted before the reboot: the previous tunnel address.
        vpn_manager._save_state(self.settings, has_config=True, rgs_tunnel_ip="10.96.0.17")

    def _tick(self, *, enabled: bool, running: bool, address: str = "10.96.0.17"):
        service = {"available": True, "enabled": enabled, "running": running}
        started = dict(service, running=True)
        with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "tunnel_is_up", return_value=True), \
                mock.patch.object(vpn_manager, "_tunnel_ip", return_value=address), \
                mock.patch.object(vpn_manager._rgs_manager, "status", return_value=service), \
                mock.patch.object(vpn_manager._rgs_manager, "start", return_value=started) as start, \
                mock.patch.object(vpn_manager._rgs_manager, "restart", return_value=started) as restart, \
                mock.patch.object(vpn_manager, "_drone_log"):
            result = vpn_manager.reconcile_rgs_with_tunnel(self.settings)
        return result, start, restart

    def test_enabled_stopped_rgs_starts_after_reboot_when_address_is_unchanged(self) -> None:
        # Reboot: VPN returns on the same 10.96.0.17 that is persisted; RGS
        # is enabled but did not start (boot-order race). Issue item 3 says it
        # must be started once the VPN is up.
        result, start, restart = self._tick(enabled=True, running=False)
        self.assertEqual(result and result.get("action"), "start")
        start.assert_called_once_with(self.settings)
        restart.assert_not_called()

    def test_disabled_rgs_is_not_started_after_reboot_with_unchanged_address(self) -> None:
        result, start, restart = self._tick(enabled=False, running=False)
        self.assertIsNone(result)
        start.assert_not_called()
        restart.assert_not_called()

    def test_running_rgs_with_unchanged_address_is_left_alone_after_reboot(self) -> None:
        result, start, restart = self._tick(enabled=True, running=True)
        self.assertIsNone(result)
        start.assert_not_called()
        restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
