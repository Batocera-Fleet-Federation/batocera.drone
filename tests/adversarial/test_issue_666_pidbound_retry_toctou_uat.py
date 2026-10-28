"""UAT for Issue #666: a pid-bound VPN revocation retry must not stop a newer tunnel."""

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


class Issue666PidboundRetryToctouUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-666-pidbound-retry",
        }, clear=True):
            self.settings = Settings.from_env()
        vpn_manager.import_from_peer(
            self.settings, _payload("old-user", "old-password"),
            source_peer_id="old-peer", source_peer_name="Old Peer",
        )

    def test_retry_does_not_stop_tunnel_started_between_pid_check_and_stop(self) -> None:
        # Issue #666 deferred finding (Issue #622 notes): "Config import after a failed stop lets
        # the poller stop the newly imported tunnel (Low)". Stage 1: the revoked tunnel (pid 42)
        # is found, but its SIGTERM raises, so the retry is bound to pid 42.
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=42), \
                mock.patch.object(vpn_manager.os, "kill", side_effect=RuntimeError("signal failed")), \
                mock.patch.object(vpn_manager, "_log_poller_failure"):
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
        failed = vpn_manager._load_state(self.settings)
        self.assertTrue(failed["disconnect_retry_pending"])
        self.assertEqual(failed["disconnect_retry_pid"], 42)

        # Stage 2: between the retry's own "is pid 42 still running?" check and the stop inside
        # disconnect(), the revoked tunnel exits and a newly connected tunnel (pid 99) appears.
        # The retry must not signal pid 99.
        lookups: list[int] = []
        signalled: list[int] = []

        def running_openvpn(_config_path, *_args, **_kwargs):
            lookups.append(1)
            if len(lookups) == 1:
                return 42  # the retry's pid-bound check still sees the revoked tunnel
            if signalled:
                return None  # the signalled tunnel has exited
            return 99  # the newer tunnel disconnect() now resolves

        def record_kill(pid, _sig):
            signalled.append(pid)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid", side_effect=running_openvpn), \
                mock.patch.object(vpn_manager.os, "kill", side_effect=record_kill), \
                mock.patch.object(vpn_manager, "_log_poller_failure"):
            vpn_manager.check_sharing_revocation(self.settings)

        self.assertNotIn(99, signalled)


if __name__ == "__main__":
    unittest.main()
