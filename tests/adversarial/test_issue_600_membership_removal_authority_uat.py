"""Issue #600: paired gossip cannot revoke a peer on behalf of another Drone.

Expected behaviour, taken from the issue and the unsigned-record invariant
before treating the implementation as truth:

- Membership records are unsigned. The only authenticated identity on a
  gossip POST is the mTLS introducer.
- A remove is honored only when it names that introducer as the peer being
  removed (self-departure), or arrives through another authorized path.
  ``origin: member`` is a body field, not proof, so it does not authorize
  removing a third-party ``source``.
- A paired member cannot force the receiver to depart (wipe its own roster).
- A far-future epoch must not evict, including on an otherwise legitimate
  self-removal, because a huge epoch would make the member unrejoinable.
- Local operator forget on one Drone must not evict that peer from every
  other roster via gossip.
- Malformed records and unpaired callers must not change the roster.
"""

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
from app.transfer.peer_connectivity import (
    _local_peer_cert_cache_path,
    _save_local_peer_certificate,
)


class MembershipRemovalAuthorityUAT(unittest.TestCase):
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

    def roster(self, settings):
        return {p["drone_id"] for p in local_network.paired_peers(settings)}

    def source_pin(self):
        return local_network.get_paired_peer(self.drones["victim"], "source")["certificate_fingerprint"]

    def source_cert_exists(self):
        return _local_peer_cert_cache_path(self.drones["victim"], "source").is_file()

    def remove_record(self, peer_id, origin, epoch=10):
        return {"peer_id": peer_id, "op": "remove", "epoch": epoch, "origin": origin, "card": {}}

    def send(self, peer_id, origin, epoch=10, extra_records=None, hop=0):
        records = [self.remove_record(peer_id, origin, epoch)]
        if extra_records:
            records.extend(extra_records)
        return swarm_membership.handle_membership_request(
            self.drones["victim"],
            {"records": records, "hop": hop},
            "member",
        )

    def assert_source_still_paired(self, pinned):
        victim = self.drones["victim"]
        self.assertIn("source", self.roster(victim))
        self.assertEqual(
            local_network.get_paired_peer(victim, "source")["certificate_fingerprint"],
            pinned,
        )
        self.assertTrue(self.source_cert_exists())
        self.assertFalse(
            any(
                r["peer_id"] == "source" and r["op"] == "remove"
                for r in swarm_membership.membership_view(victim)["records"]
            )
        )

    def test_third_party_and_missing_origin_removals_leave_roster_and_pin_intact(self):
        pinned = self.source_pin()
        for origin in ("source", "someone-else", ""):
            self.send("source", origin)
            self.assertEqual(self.roster(self.drones["victim"]), {"member", "source"})
            self.assertEqual(
                local_network.get_paired_peer(self.drones["victim"], "source")["certificate_fingerprint"],
                pinned,
            )
        self.assertFalse(any(
            r["peer_id"] == "source" and r["op"] == "remove"
            for r in swarm_membership.membership_view(self.drones["victim"])["records"]
        ))

    def test_far_future_epoch_cannot_evict_even_when_claimed_by_introducer(self):
        victim = self.drones["victim"]
        self.send("source", "member", 1_000_000_000)
        self.assertEqual(self.roster(victim), {"member", "source"})
        self.assertIsNotNone(local_network.get_paired_peer(victim, "source"))

    def test_introducer_can_still_remove_itself_from_another_roster(self):
        self.send("member", "member", 10)
        self.assertEqual(self.roster(self.drones["victim"]), {"source"})

    def test_stale_authenticated_self_removal_does_not_undo_newer_pairing(self):
        """A real sender identity does not make an old departure current."""
        victim = self.drones["victim"]
        member = local_network.get_paired_peer(victim, "member")
        pinned = member["certificate_fingerprint"]
        current = next(
            record
            for record in swarm_membership.membership_view(victim)["records"]
            if record["peer_id"] == "member"
        )

        self.send("member", "member", max(0, current["epoch"] - 1))

        self.assertIn("member", self.roster(victim))
        self.assertEqual(
            local_network.get_paired_peer(victim, "member")["certificate_fingerprint"],
            pinned,
        )

    def test_introducer_can_still_announce_its_own_removal_of_a_peer(self):
        """Adjudicated against issue #600.

        The prior assertion treated ``origin: member`` plus mTLS from
        ``member`` as authority to forget third-party ``source``. The issue
        names that as the vulnerability: records are unsigned, so only a
        remove whose ``peer_id`` is the authenticated introducer (or another
        authorized path) may be honored. ``member`` removing ``source`` is
        not self-departure.
        """
        pinned = self.source_pin()
        response = self.send("source", "member", 10)
        self.assertEqual(response.get("status"), "synced")
        self.assertEqual(self.roster(self.drones["victim"]), {"member", "source"})
        self.assert_source_still_paired(pinned)

    def test_introducer_cannot_force_receiver_to_depart(self):
        pinned = self.source_pin()
        self.send("victim", "member", 10)
        self.assertEqual(self.roster(self.drones["victim"]), {"member", "source"})
        self.assert_source_still_paired(pinned)
        self.assertFalse(swarm_membership._departed(self.drones["victim"]))

    def test_spoofed_receiver_origin_cannot_force_departure(self):
        self.send("victim", "victim", 10)
        self.assertEqual(self.roster(self.drones["victim"]), {"member", "source"})
        self.assertFalse(swarm_membership._departed(self.drones["victim"]))

    def test_far_future_epoch_cannot_self_remove(self):
        self.send("member", "member", 1_000_000_000)
        self.assertEqual(self.roster(self.drones["victim"]), {"member", "source"})
        self.assertIsNotNone(local_network.get_paired_peer(self.drones["victim"], "member"))

    def test_mixed_payload_self_remove_does_not_take_a_third_party(self):
        pinned = self.source_pin()
        swarm_membership.handle_membership_request(
            self.drones["victim"],
            {"records": [
                self.remove_record("source", "member", 10),
                self.remove_record("member", "member", 10),
            ]},
            "member",
        )
        self.assertEqual(self.roster(self.drones["victim"]), {"source"})
        self.assert_source_still_paired(pinned)

    def test_malformed_records_do_not_evict(self):
        pinned = self.source_pin()
        victim = self.drones["victim"]
        payloads = [
            {"records": None},
            {"records": {"peer_id": "source", "op": "remove", "epoch": 10, "origin": "member"}},
            {"records": "remove source"},
            {"records": [self.remove_record("source", "member", True)]},
            {"records": [self.remove_record("source", "member", -1)]},
            {"records": [{"peer_id": "source", "op": "REMOVE", "epoch": 10, "origin": "member"}]},
            {"records": [{"peer_id": "source", "op": "remove", "epoch": "10", "origin": "member"}]},
            {"records": [{"peer_id": "x" * 129, "op": "remove", "epoch": 10, "origin": "member"}]},
            {"records": [self.remove_record("source", "member", 1_000_000_001)]},
        ]
        for payload in payloads:
            swarm_membership.handle_membership_request(victim, payload, "member")
            self.assertEqual(self.roster(victim), {"member", "source"}, payload)
        self.assert_source_still_paired(pinned)

    def test_non_object_payload_is_rejected_without_roster_change(self):
        with self.assertRaises(swarm_membership.MembershipRejected) as raised:
            swarm_membership.handle_membership_request(
                self.drones["victim"],
                [self.remove_record("source", "member")],
                "member",
            )
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(self.roster(self.drones["victim"]), {"member", "source"})

    def test_unpaired_caller_cannot_remove_anyone(self):
        with self.assertRaises(swarm_membership.MembershipRejected) as raised:
            swarm_membership.handle_membership_request(
                self.drones["victim"],
                {"records": [self.remove_record("source", "stranger")]},
                "stranger",
            )
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(self.roster(self.drones["victim"]), {"member", "source"})

    def test_max_hop_third_party_remove_is_still_ignored(self):
        pinned = self.source_pin()
        self.send("source", "member", 10, hop=swarm_membership.MAX_FORWARD_HOP)
        self.assert_source_still_paired(pinned)

    def test_publish_forget_does_not_evict_third_party_from_other_rosters(self):
        drones = self.drones

        def poster(settings, peer, payload):
            target = drones.get(str(peer.get("drone_id") or ""))
            if target is None:
                raise ConnectionError("missing peer")
            return swarm_membership.handle_membership_request(
                target, payload, str(settings.device_id),
            )

        swarm_membership.set_membership_poster(poster)
        pinned = self.source_pin()
        self.assertTrue(swarm_membership.publish_forget(drones["member"], "source"))
        self.assertNotIn("source", self.roster(drones["member"]))
        self.assertEqual(self.roster(drones["victim"]), {"member", "source"})
        self.assert_source_still_paired(pinned)
        self.assertIn("member", self.roster(drones["source"]))
        self.assertIn("victim", self.roster(drones["source"]))
        self.assertFalse(swarm_membership._departed(drones["source"]))
        self.assertFalse(swarm_membership._departed(drones["victim"]))

    def test_removed_status_response_cannot_empty_local_roster(self):
        def poster(settings, peer, payload):
            return {
                "status": "removed",
                "records": [self.remove_record("victim", "member", 99)],
            }

        swarm_membership.set_membership_poster(poster)
        swarm_membership.propagate_membership(self.drones["victim"])
        self.assertEqual(self.roster(self.drones["victim"]), {"member", "source"})
        self.assertFalse(swarm_membership._departed(self.drones["victim"]))


if __name__ == "__main__":
    unittest.main()
