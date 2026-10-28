"""Issue #635: re-pairing after forget must survive the first membership POST."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, swarm_membership
from app.transfer.drone_tls import DroneCertificateManager
from app.transfer.peer_connectivity import _save_local_peer_certificate


class RepairFirstGossipUAT(unittest.TestCase):
    def setUp(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required to mint pairing certificates")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"HTTP_ONLY": "1"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(lambda: swarm_membership.set_membership_poster(None))
        self.victim = self.make_drone("victim")
        self.member = self.make_drone("member")
        self.source = self.make_drone("source")

    def make_drone(self, name):
        root = Path(self.tmp.name) / name
        for folder in ("roms", "bios", "saves", "movies"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        env = {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
            "MOVIES_ROOT": str(root / "movies"), "DRONE_DEVICE_ID": name,
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

    def test_first_post_after_repair_is_accepted(self):
        self.approve(self.victim, self.source)
        self.approve(self.victim, self.member)
        self.approve(self.member, self.victim)
        self.assertTrue(swarm_membership.publish_forget(self.victim, "member"))

        # Match the pairing handlers: persist the pin, then record explicit approval.
        self.approve(self.victim, self.member)

        # The first POST must not receive the issue's reported 403.
        swarm_membership.handle_membership_request(self.victim, {"records": []}, "member")
        self.assertIn("member", {p["drone_id"] for p in local_network.paired_peers(self.victim)})


if __name__ == "__main__":
    unittest.main()
