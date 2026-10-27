"""Issue #600: a gossiped third-party removal must not trigger peer-gone wipes.

The attack in the issue: paired member M gossips ``{"peer_id": S, "op":
"remove"}``. Victim V forgets S, then the VPN/SMTP/tailnet revocation tick
sees ``get_paired_peer(S) is None`` and deletes credentials V imported from
S. Expected behaviour from the issue: that unsigned third-party remove is
not authority, so the roster entry and the imported secrets stay.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.smtp_manager as smtp_manager
import app.device.tailnet_service as tailnet_service
import app.device.vpn_manager as vpn_manager
from app.common.settings import Settings
from app.transfer import local_network, swarm_membership
from app.transfer.drone_tls import DroneCertificateManager
from app.transfer.peer_connectivity import _save_local_peer_certificate


OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"
SMTP_PAYLOAD = {
    "host": "smtp.example.com",
    "port": 587,
    "use_starttls": True,
    "use_ssl": False,
    "username": "me@example.com",
    "password": "smtp-secret-600",
    "from_address": "drone@example.com",
    "recipient_email": "owner@example.com",
}


class Issue600PeerGoneCredentialWipeUAT(unittest.TestCase):
    def setUp(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required to mint deterministic test certificates")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {"HTTP_ONLY": "1", "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1"},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(lambda: swarm_membership.set_membership_poster(None))
        self.drones = {name: self.make_drone(name) for name in ("victim", "member", "source")}
        for name, settings in self.drones.items():
            for other_name, other in self.drones.items():
                if name != other_name:
                    self.approve(settings, other)
        victim_root = Path(self.tmp.name) / "victim"
        install = mock.patch.object(
            vpn_manager, "_drone_install_root", return_value=victim_root / "install",
        )
        install.start()
        self.addCleanup(install.stop)

    def make_drone(self, name):
        root = Path(self.tmp.name) / name
        for folder in ("roms", "bios", "saves", "movies"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        env = {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "MOVIES_ROOT": str(root / "movies"),
            "DRONE_DEVICE_ID": name,
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite"),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            settings = Settings.from_env()
        DroneCertificateManager(settings).ensure_certificate()
        return settings

    def approve(self, settings, other):
        cert = DroneCertificateManager(other).ensure_certificate()
        pem = str(cert["public_certificate"])
        path, fingerprint = _save_local_peer_certificate(settings, other.device_id, pem)
        peer = local_network.save_paired_peer(settings, {
            "drone_id": other.device_id, "name": other.device_id,
            "reachable_url": "https://127.0.0.1/", "scheme": "https",
            "certificate_fingerprint": fingerprint, "certificate_path": str(path),
            "certificate_pem": pem, "pairing_source": "local_network",
        })
        swarm_membership.record_approved_member(settings, peer)

    def gossip_third_party_remove(self):
        return swarm_membership.handle_membership_request(
            self.drones["victim"],
            {"records": [{
                "peer_id": "source", "op": "remove", "epoch": 10,
                "origin": "member", "card": {},
            }]},
            "member",
        )

    def test_rejected_third_party_remove_does_not_wipe_vpn_imported_from_source(self):
        victim = self.drones["victim"]
        vpn_manager.import_from_peer(
            victim,
            {
                "config_filename": "client.ovpn", "config_text": OVPN,
                "has_credentials": True, "username": "peeruser", "password": "peerpass123",
            },
            source_peer_id="source", source_peer_name="source",
        )
        self.assertTrue(vpn_manager.auth_path(victim).exists())
        self.gossip_third_party_remove()
        self.assertIsNotNone(local_network.get_paired_peer(victim, "source"))
        with mock.patch.object(vpn_manager, "disconnect") as disconnect, \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer",
                           return_value=({"ok": True}, "https://source/")):
            self.assertFalse(vpn_manager.check_sharing_revocation(victim))
        disconnect.assert_not_called()
        self.assertTrue(vpn_manager.auth_path(victim).exists())
        state = vpn_manager._load_state(victim)
        self.assertTrue(state["has_credentials"])
        self.assertEqual(state["username"], "peeruser")
        self.assertEqual(state["revoked_reason"], "")
        self.assertEqual(state["source_peer_id"], "source")

    def test_rejected_third_party_remove_does_not_wipe_smtp_imported_from_source(self):
        victim = self.drones["victim"]
        smtp_manager.import_from_peer(
            victim, SMTP_PAYLOAD, source_peer_id="source", source_peer_name="source",
        )
        self.gossip_third_party_remove()
        with mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer",
                        return_value=({"ok": True}, "https://source/")):
            self.assertFalse(smtp_manager.check_sharing_revocation(victim))
        state = smtp_manager._load_state(victim)
        self.assertEqual(state["password"], "smtp-secret-600")
        self.assertEqual(state["revoked_reason"], "")
        self.assertEqual(state["source_peer_id"], "source")
        self.assertTrue(state["has_config"])

    def test_rejected_third_party_remove_does_not_wipe_tailnet_key_imported_from_source(self):
        victim = self.drones["victim"]

        def fake_enroll(auth_key, settings=None):
            tailnet_service._save_sharing_state(settings, auth_key=auth_key)
            return {"ok": True}

        with mock.patch.object(tailnet_service, "tailnet_enroll", side_effect=fake_enroll):
            tailnet_service.import_tailnet_from_peer(
                victim, {"auth_key": "tskey-auth-issue-600"},
                source_peer_id="source", source_peer_name="source",
            )
        self.gossip_third_party_remove()
        with mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer",
                        return_value=({"ok": True}, "https://source/")):
            self.assertFalse(tailnet_service.check_tailnet_sharing_revocation(victim))
        state = tailnet_service._load_sharing_state(victim)
        self.assertEqual(state["auth_key"], "tskey-auth-issue-600")
        self.assertEqual(state["revoked_reason"], "")
        self.assertEqual(state["source_peer_id"], "source")

    def test_authenticated_self_removal_still_wipes_credentials_imported_from_that_peer(self):
        victim = self.drones["victim"]
        vpn_manager.import_from_peer(
            victim,
            {
                "config_filename": "client.ovpn", "config_text": OVPN,
                "has_credentials": True, "username": "member-user", "password": "member-pass",
            },
            source_peer_id="member", source_peer_name="member",
        )
        swarm_membership.handle_membership_request(
            victim,
            {"records": [{
                "peer_id": "member", "op": "remove", "epoch": 10,
                "origin": "member", "card": {},
            }]},
            "member",
        )
        self.assertIsNone(local_network.get_paired_peer(victim, "member"))
        with mock.patch.object(vpn_manager, "disconnect"):
            self.assertTrue(vpn_manager.check_sharing_revocation(victim))
        self.assertFalse(vpn_manager.auth_path(victim).exists())
        state = vpn_manager._load_state(victim)
        self.assertFalse(state["has_credentials"])
        self.assertEqual(state["revoked_reason"], vpn_manager._SHARING_REVOKED_PEER_GONE)
        self.assertEqual(state["source_peer_id"], "member")


if __name__ == "__main__":
    unittest.main()
