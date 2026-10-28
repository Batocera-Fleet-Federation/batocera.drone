"""UAT for Issue #666's deferred PID-less VPN retry finding."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"


def _payload(username: str, password: str) -> dict:
    return {
        "config_filename": "client.ovpn",
        "config_text": OVPN,
        "has_credentials": True,
        "username": username,
        "password": password,
    }


class Issue666PidlessRetryReimportUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install")
        install.start()
        self.addCleanup(install.stop)
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-666-pidless-retry",
        }, clear=True):
            self.settings = Settings.from_env()
        vpn_manager.import_from_peer(
            self.settings, _payload("old-user", "old-password"),
            source_peer_id="old-peer", source_peer_name="Old Peer",
        )

    def test_pidless_retry_never_stops_reimported_tunnel_started_milliseconds_later(self) -> None:
        # Issue #666 unresolved finding: "Retry after a PID-less disconnect exception can stop a
        # newly imported tunnel." A process created even milliseconds after the failed revoke is
        # a newer re-imported tunnel, so the subsequent retry must not call disconnect().
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", side_effect=RuntimeError("pid unavailable")), \
                mock.patch.object(vpn_manager, "_log_poller_failure"):
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))

        failed = vpn_manager._load_state(self.settings)
        self.assertTrue(failed["disconnect_retry_pending"])
        self.assertIsNone(failed["disconnect_retry_pid"])
        self.assertIsNotNone(failed["disconnect_retry_since"])

        vpn_manager.import_from_peer(
            self.settings, _payload("new-user", "new-password"),
            source_peer_id="new-peer", source_peer_name="New Peer",
        )
        self.assertEqual(vpn_manager._load_state(self.settings)["source_peer_id"], "new-peer")

        with mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=99), \
                mock.patch.object(
                    vpn_manager, "_process_started_at",
                    return_value=failed["disconnect_retry_since"] + 0.1,
                ), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
        disconnect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
