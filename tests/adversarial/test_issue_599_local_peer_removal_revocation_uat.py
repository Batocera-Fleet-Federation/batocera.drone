"""Boundary UAT for #599: local peer removal remains valid revocation evidence.

The issue distinguishes a forged network 404 from the local roster saying the
source is no longer paired.  This test checks all three imported credential
types against that distinction: with no paired record, no request is needed
and the imported secrets are revoked as before.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.smtp_manager as smtp_manager
import app.device.tailnet_service as tailnet_service
import app.device.vpn_manager as vpn_manager
from app.common.settings import Settings
from app.transfer import peer_connectivity as pc


SOURCE = "source-removed-599"
OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"
SMTP = {
    "host": "smtp.example.com", "port": 587, "use_starttls": True,
    "use_ssl": False, "username": "me@example.com", "password": "smtp-599",
    "from_address": "drone@example.com", "recipient_email": "owner@example.com",
}


def _settings(root: Path) -> Settings:
    with mock.patch.dict("os.environ", {
        "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "adversarial-599-peer-removal",
    }, clear=True):
        return Settings.from_env()


class Issue599LocalPeerRemovalUAT(unittest.TestCase):
    def test_local_unpair_revokes_imported_vpn_smtp_and_tailnet_credentials(self) -> None:
        with tempfile.TemporaryDirectory(prefix="issue-599-unpair-") as directory:
            root = Path(directory)
            settings = _settings(root)
            with mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install"), \
                    mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=True):
                vpn_manager.import_from_peer(
                    settings,
                    {"config_filename": "client.ovpn", "config_text": OVPN,
                     "has_credentials": True, "username": "vpn-user", "password": "vpn-pass"},
                    source_peer_id=SOURCE, source_peer_name="Removed Source",
                )
                smtp_manager.import_from_peer(settings, SMTP, source_peer_id=SOURCE,
                                              source_peer_name="Removed Source")

                cli = mock.MagicMock()
                cli.exists.return_value = True
                cli.__str__.return_value = "/userdata/system/tailscale/bin/tailscale"
                status = {"BackendState": "Running", "Version": "1.70.0",
                          "Self": {"TailscaleIPs": ["100.64.0.5"], "DNSName": "node.ts.net.", "ID": "self"},
                          "CurrentTailnet": {"Name": "example.ts.net", "MagicDNSSuffix": "ts.net"}, "Peer": {}}
                result = mock.Mock(returncode=0, stdout=json.dumps(status), stderr="")

                def tailscale_run(args, **_kwargs):
                    if "status" in args and "--json" in args:
                        return result
                    return mock.Mock(returncode=0, stdout="", stderr="")

                with mock.patch.object(tailnet_service, "TAILSCALE_CLI", cli), \
                        mock.patch.object(tailnet_service.subprocess, "run", side_effect=tailscale_run):
                    tailnet_service.import_tailnet_from_peer(
                        settings, {"auth_key": "tskey-auth-599", "enrolled": True},
                        source_peer_id=SOURCE, source_peer_name="Removed Source",
                    )

                with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                        mock.patch.object(pc, "_peer_get_json_for_peer") as get_peer, \
                        mock.patch.object(vpn_manager, "disconnect") as disconnect:
                    self.assertTrue(vpn_manager.check_sharing_revocation(settings))
                    self.assertTrue(smtp_manager.check_sharing_revocation(settings))
                    self.assertTrue(tailnet_service.check_tailnet_sharing_revocation(settings))

            get_peer.assert_not_called()
            disconnect.assert_called_once()
            self.assertFalse(vpn_manager._load_state(settings)["has_credentials"])
            self.assertFalse(vpn_manager.auth_path(settings).exists())
            self.assertIn("no longer paired", vpn_manager._load_state(settings)["revoked_reason"])
            self.assertEqual(smtp_manager._load_state(settings)["password"], "")
            self.assertIn("no longer paired", smtp_manager._load_state(settings)["revoked_reason"])
            self.assertEqual(tailnet_service._load_sharing_state(settings)["auth_key"], "")
            self.assertIn("no longer paired", tailnet_service._load_sharing_state(settings)["revoked_reason"])


if __name__ == "__main__":
    unittest.main()
