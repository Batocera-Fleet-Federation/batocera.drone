"""Issue #600: paired gossip cannot revoke a peer on behalf of another Drone."""

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


class MembershipRemovalAuthorityUAT(unittest.TestCase):
    def setUp(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required to mint deterministic test certificates")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"HTTP_ONLY": "1", "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(lambda: swarm_membership.set_membership_poster(None))
        self.drones = {name: self.make_drone(name) for name in ("victim", "member", "source")}
        for name, settings in self.drones.items():
            for other_name, other in self.drones.items():
                if name != other_name:
                    self.approve(settings, other)

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

    def roster(self, settings):
        return {p["drone_id"] for p in local_network.paired_peers(settings)}

    def send(self, peer_id, origin, epoch=10):
        return swarm_membership.handle_membership_request(
            self.drones["victim"],
            {"records": [{"peer_id": peer_id, "op": "remove", "epoch": epoch,
                           "origin": origin, "card": {}}]},
            "member",
        )

    def test_third_party_and_missing_origin_removals_leave_roster_and_pin_intact(self):
        victim = self.drones["victim"]
        pinned = local_network.get_paired_peer(victim, "source")["certificate_fingerprint"]
        for origin in ("source", "someone-else", ""):
            self.send("source", origin)
            self.assertEqual(self.roster(victim), {"member", "source"})
            self.assertEqual(local_network.get_paired_peer(victim, "source")["certificate_fingerprint"], pinned)
        self.assertFalse(any(r["peer_id"] == "source" and r["op"] == "remove"
                             for r in swarm_membership.membership_view(victim)["records"]))

    def test_far_future_epoch_cannot_evict_even_when_claimed_by_introducer(self):
        victim = self.drones["victim"]
        self.send("source", "member", 1_000_000_000)
        self.assertEqual(self.roster(victim), {"member", "source"})
        self.assertIsNotNone(local_network.get_paired_peer(victim, "source"))

    def test_introducer_can_still_remove_itself_from_another_roster(self):
        self.send("member", "member", 10)
        self.assertEqual(self.roster(self.drones["victim"]), {"source"})

    def test_introducer_can_still_announce_its_own_removal_of_a_peer(self):
        self.send("source", "member", 10)
        self.assertEqual(self.roster(self.drones["victim"]), {"member"})


if __name__ == "__main__":
    unittest.main()
