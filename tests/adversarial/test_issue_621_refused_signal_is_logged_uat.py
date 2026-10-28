"""Issue #621 UAT for refused tunnel-stop signals during VPN revocation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class RefusedSignalRevocationUAT(unittest.TestCase):
    def _settings(self, root: Path) -> Settings:
        env = {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "issue-621-test",
        }
        patcher = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install")
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.dict("os.environ", env, clear=True):
            settings = Settings.from_env()
        vpn_manager.save_uploaded_config(settings, "client.ovpn", b"client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n")
        vpn_manager.save_credentials(settings, "user", "secret")
        vpn_manager._save_state(settings, source_peer_id="peer-1", source_peer_name="Peer")
        return settings

    def test_permission_error_status_is_logged_while_credentials_are_revoked(self) -> None:
        # Issue #621: "Refused SIGTERM during VPN revocation is returned as an error dict and never logged"
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            with mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=321), \
                    mock.patch.object(vpn_manager.os, "kill", side_effect=PermissionError("signal denied")), \
                    mock.patch.object(vpn_manager, "_log_poller_failure") as logged:
                vpn_manager._revoke_local_credentials(settings, "revoked by peer")

            logged.assert_called_once()
            self.assertEqual(logged.call_args.args[0], "VPN sharing revocation check failed")
            self.assertIn("signal denied", str(logged.call_args.args[1]))
            state = vpn_manager._load_state(settings)
            self.assertFalse(state["has_credentials"])
            self.assertTrue(state["disconnect_retry_pending"])
            self.assertFalse(vpn_manager.auth_path(settings).exists())

    def test_error_status_on_retry_is_logged(self) -> None:
        # Issue #621's refused-signal error dict must remain visible on retry ticks too.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            vpn_manager._save_state(settings, has_credentials=False, disconnect_retry_pending=True)
            failure = {"status": "error", "errors": ["signal denied"]}
            with mock.patch.object(vpn_manager, "disconnect", return_value=failure), \
                    mock.patch.object(vpn_manager, "_log_poller_failure") as logged:
                self.assertFalse(vpn_manager.check_sharing_revocation(settings))
            logged.assert_called_once()
            self.assertEqual(logged.call_args.args[0], "VPN sharing revocation check failed")
            self.assertIn("signal denied", str(logged.call_args.args[1]))
            self.assertTrue(vpn_manager._load_state(settings)["disconnect_retry_pending"])


if __name__ == "__main__":
    unittest.main()
