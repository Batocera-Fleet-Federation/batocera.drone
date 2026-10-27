"""Issue #600 boundary/UAT checks beyond the first two suites.

Expected behaviour is derived from the issue and from the unsigned-record
invariant, not from the implementation:

- The only authenticated identity on a gossip exchange is the paired peer on
  the other end. A forged third-party removal is therefore not authority, over
  the real HTTP route as well as the in-process entry point, and in a sync
  *reply* as well as in a request.
- A rejected record must have no side effects: no clock poisoning, no stored
  tombstone, no dirty flag, and no extra outbound traffic (amplification).
- Rejecting forged removals must not break the legitimate paths: a member can
  still leave, can rejoin after an explicit re-pair, and concurrent forged
  removals cannot lose a legitimate one.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from app.common.auth import DroneCredentialStore, SessionAuth, SessionStore
from app.common.http_cache import ExpiringKeyCache, ExpiringLRUCache
from app.common.settings import Settings
from app.drone_api import DroneThreadingHTTPServer, RomRepository, _build_handler
from app.storage.state_store import database_path
from app.transfer import local_network, swarm_membership
from app.transfer.drone_tls import DroneCertificateManager
from app.transfer.peer_connectivity import _local_peer_cert_cache_path, _save_local_peer_certificate


def _free_port() -> int:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


class _Base(unittest.TestCase):
    NAMES = ("victim", "member", "source")

    def setUp(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required to mint deterministic test certificates")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(
            os.environ,
            {"HTTP_ONLY": "1", "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1"},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(lambda: swarm_membership.set_membership_poster(None))
        swarm_membership.set_membership_poster(None)
        self.drones = {name: self.make_drone(name) for name in self.NAMES}
        for name, settings in self.drones.items():
            for other_name, other in self.drones.items():
                if name != other_name:
                    self.approve(settings, other)

    def make_drone(self, name, port=None):
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
        if port:
            env.update({
                "HTTPS_PORT": str(port),
                "DRONE_ADVERTISED_API_PORT": str(port),
                "DRONE_PEER_MTLS_PORT": str(port),
                "DRONE_ADVERTISED_PEER_MTLS_PORT": str(port),
                "LOG_DIR": str(root / "logs"),
            })
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

    @staticmethod
    def remove(peer_id, origin, epoch=10):
        return {"peer_id": peer_id, "op": "remove", "epoch": epoch, "origin": origin, "card": {}}

    def clock(self, settings):
        return swarm_membership.membership_view(settings)["clock"]

    def assert_intact(self, settings, expected=("member", "source")):
        self.assertEqual(self.roster(settings), set(expected))
        self.assertFalse(any(
            r["op"] == "remove"
            for r in swarm_membership.membership_view(settings)["records"]
        ), "a rejected removal must not leave a tombstone behind")


class RejectedRecordsHaveNoSideEffectsUAT(_Base):
    def test_rejected_far_future_third_party_removal_does_not_poison_the_clock(self):
        victim = self.drones["victim"]
        before = self.clock(victim)
        for epoch in (before + 5_000_000, 999_999_999, 1_000_000_000):
            swarm_membership.handle_membership_request(
                victim, {"records": [self.remove("source", "member", epoch)]}, "member",
            )
        self.assertEqual(self.clock(victim), before)
        self.assert_intact(victim)
        # The victim's own later events still mint small, ordinary epochs.
        self.assertTrue(swarm_membership.publish_forget(victim, "source"))
        self.assertLess(self.clock(victim), before + 10)

    def test_rejected_far_future_self_removal_does_not_poison_the_clock(self):
        victim = self.drones["victim"]
        before = self.clock(victim)
        swarm_membership.handle_membership_request(
            victim, {"records": [self.remove("member", "member", 1_000_000_000)]}, "member",
        )
        self.assertEqual(self.clock(victim), before)
        self.assert_intact(victim)

    def test_forged_removals_cause_no_outbound_amplification(self):
        victim = self.drones["victim"]
        calls = []

        def poster(settings, peer, payload):
            calls.append(peer["drone_id"])
            return {"status": "synced", "records": []}

        swarm_membership.set_membership_poster(poster)
        swarm_membership.propagate_membership(victim)  # flush the setup-time dirty flag
        self.assertFalse(swarm_membership._is_dirty(victim))
        calls.clear()
        for origin in ("member", "source", "victim", ""):
            swarm_membership.handle_membership_request(
                victim, {"records": [self.remove("source", origin, 11)], "hop": 0}, "member",
            )
        self.assertEqual(calls, [], "rejected records must not make the victim forward anything")
        self.assertFalse(swarm_membership._is_dirty(victim))
        self.assert_intact(victim)

    def test_forged_removal_returns_a_reply_with_no_removal_authority(self):
        victim = self.drones["victim"]
        response = swarm_membership.handle_membership_request(
            victim, {"records": [self.remove("source", "member", 11)]}, "member",
        )
        self.assertEqual(response["status"], "synced")
        self.assertEqual(
            {(r["peer_id"], r["op"]) for r in response["records"]},
            {("member", "add"), ("source", "add")},
        )

    def test_one_forged_removal_does_not_block_other_records_in_the_payload(self):
        """A rejected record is skipped, not a reason to drop the whole payload."""
        victim = self.drones["victim"]
        newcomer = self.make_drone("newcomer")
        cert = DroneCertificateManager(newcomer).ensure_certificate()
        add = {
            "peer_id": "newcomer", "op": "add", "epoch": 50, "origin": "member",
            "card": {
                "name": "newcomer", "reachable_url": "https://127.0.0.1/", "scheme": "https",
                "certificate_pem": str(cert["public_certificate"]),
                "certificate_fingerprint": str(cert["fingerprint"]),
            },
        }
        swarm_membership.handle_membership_request(
            victim, {"records": [self.remove("source", "member", 49), add]}, "member",
        )
        self.assertEqual(self.roster(victim), {"member", "source", "newcomer"})


class ReplySideForgeryUAT(_Base):
    """The same forgery arriving as a sync *reply* (we dialed the attacker)."""

    def run_sync(self, victim, reply_for):
        contacted = []

        def poster(settings, peer, payload):
            contacted.append(peer["drone_id"])
            return reply_for(peer["drone_id"])

        swarm_membership.set_membership_poster(poster)
        swarm_membership.propagate_membership(victim)
        return contacted

    def test_reply_with_third_party_removal_is_ignored(self):
        victim = self.drones["victim"]
        before = self.clock(victim)
        contacted = self.run_sync(victim, lambda who: {
            "status": "synced",
            "records": [self.remove("source" if who == "member" else "member", who, 10)],
        })
        self.assertEqual(set(contacted), {"member", "source"})
        self.assert_intact(victim)
        self.assertEqual(self.clock(victim), before)

    def test_reply_removal_naming_a_third_party_with_far_future_epoch_is_ignored(self):
        victim = self.drones["victim"]
        self.run_sync(victim, lambda who: {
            "status": "synced",
            "records": [self.remove("source", who, 1_000_000_000)],
        })
        self.assert_intact(victim)

    def test_removed_status_reply_with_forged_third_party_record_changes_nothing(self):
        victim = self.drones["victim"]
        contacted = self.run_sync(victim, lambda who: {
            "status": "removed",
            "records": [self.remove("source", "member", 10), self.remove("victim", "member", 10)],
        })
        self.assertEqual(set(contacted), {"member", "source"})
        self.assert_intact(victim)
        self.assertFalse(swarm_membership._departed(victim))

    def test_reply_cannot_make_a_responder_remove_a_different_responder(self):
        """member answering 'source is gone' must not evict source, even though
        source is itself a paired responder in the same round."""
        victim = self.drones["victim"]
        self.run_sync(victim, lambda who: {
            "status": "synced",
            "records": [self.remove("source", "member", 10)] if who == "member" else [],
        })
        self.assert_intact(victim)

    def test_reply_self_removal_by_the_responder_is_still_honoured(self):
        victim = self.drones["victim"]
        self.run_sync(victim, lambda who: {
            "status": "synced",
            "records": [self.remove("member", "member", 10)] if who == "member" else [],
        })
        self.assertEqual(self.roster(victim), {"source"})


class LegitimateDepartureStillWorksUAT(_Base):
    def test_self_removed_member_can_rejoin_after_an_explicit_repair(self):
        victim = self.drones["victim"]
        member = self.drones["member"]
        swarm_membership.handle_membership_request(
            victim, {"records": [self.remove("member", "member", self.clock(victim) + 1000)]}, "member",
        )
        self.assertEqual(self.roster(victim), {"source"})
        # Not paired any more: a further call is refused / told it was removed.
        reply = swarm_membership.handle_membership_request(victim, {"records": []}, "member")
        self.assertEqual(reply["status"], "removed")
        # An explicit new approval is a new join and must outrank the tombstone.
        self.approve(victim, member)
        # Any state access reconciles the roster with the new add record. (The
        # instant between the pairing write and that reconcile is pre-existing
        # behaviour unrelated to #600 and deliberately not asserted here.)
        view = swarm_membership.membership_view(victim)
        self.assertEqual(
            {(r["peer_id"], r["op"]) for r in view["records"]},
            {("member", "add"), ("source", "add")},
        )
        self.assertEqual(self.roster(victim), {"member", "source"})
        swarm_membership.handle_membership_request(victim, {"records": []}, "member")
        self.assertEqual(self.roster(victim), {"member", "source"})

    def test_removal_at_the_edge_of_normal_clock_skew_is_honoured(self):
        victim = self.drones["victim"]
        swarm_membership.handle_membership_request(
            victim, {"records": [self.remove("member", "member", self.clock(victim) + 1000)]}, "member",
        )
        self.assertEqual(self.roster(victim), {"source"})

    def test_cert_of_self_removed_member_is_unlinked_but_source_cert_kept(self):
        victim = self.drones["victim"]
        swarm_membership.handle_membership_request(
            victim, {"records": [self.remove("member", "member", 10)]}, "member",
        )
        self.assertFalse(_local_peer_cert_cache_path(victim, "member").exists())
        self.assertTrue(_local_peer_cert_cache_path(victim, "source").is_file())

    def test_operator_forget_then_stale_add_gossip_does_not_resurrect_peer(self):
        victim = self.drones["victim"]
        stale = swarm_membership.membership_view(self.drones["member"])["records"]
        stale = [r for r in stale if r["peer_id"] == "source" and r["op"] == "add"]
        self.assertTrue(stale)
        self.assertTrue(swarm_membership.publish_forget(victim, "source"))
        swarm_membership.handle_membership_request(victim, {"records": stale}, "member")
        self.assertEqual(self.roster(victim), {"member"})

    def test_concurrent_forged_removals_cannot_mask_a_legitimate_one(self):
        victim = self.drones["victim"]
        errors = []

        def forged():
            try:
                for epoch in range(10, 30):
                    swarm_membership.handle_membership_request(
                        victim, {"records": [self.remove("source", "member", epoch)]}, "member",
                    )
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)

        def legit():
            try:
                swarm_membership.handle_membership_request(
                    victim, {"records": [self.remove("member", "member", 10)]}, "member",
                )
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=forged) for _ in range(4)] + [threading.Thread(target=legit)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(self.roster(victim), {"source"})
        self.assertTrue(_local_peer_cert_cache_path(victim, "source").is_file())


class HttpRouteUAT(unittest.TestCase):
    """The real ``POST /v1/api/peer/membership`` route, not just the function."""

    def setUp(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required to mint deterministic test certificates")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(
            os.environ,
            {
                "HTTP_ONLY": "1", "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1",
                "DRONE_COMPAT_HTTPS_PORTS": "", "ROM_METADATA_POLL_SECONDS": "0",
                "DRONE_CAST_ENABLED": "0", "DRONE_HTTP_REDIRECT_PORT": "0", "USE_FAKE_DATA": "0",
            },
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(lambda: swarm_membership.set_membership_poster(None))
        swarm_membership.set_membership_poster(lambda *_args: {"records": []})
        helper = _Base("run")
        helper.tmp = self.tmp
        self.helper = helper
        self.port = _free_port()
        self.victim = helper.make_drone("victim", port=self.port)
        self.member = helper.make_drone("member")
        self.source = helper.make_drone("source")
        for other in (self.member, self.source):
            helper.approve(self.victim, other)
        helper.approve(self.member, self.victim)
        helper.approve(self.source, self.victim)
        helper.approve(self.member, self.source)
        helper.approve(self.source, self.member)
        database = database_path(self.victim.userdata_root)
        auth = SessionAuth(
            DroneCredentialStore(self.victim.credentials_file, state_database_file=database),
            SessionStore(database),
        )
        handler = _build_handler(
            self.victim, auth,
            RomRepository(self.victim.roms_root, self.victim.bios_root, settings=self.victim),
            ExpiringLRUCache(60, 8, 1024), ExpiringKeyCache(60), ExpiringLRUCache(60, 8, 1024),
        )
        self.server = DroneThreadingHTTPServer(("127.0.0.1", self.port), handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()

        self.addCleanup(stop)

    def post(self, body, introducer="member"):
        payload = dict(body, introducer_id=introducer)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/api/peer/membership",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8") or "{}")

    def roster(self):
        return {p["drone_id"] for p in local_network.paired_peers(self.victim)}

    def test_http_forged_third_party_removal_is_not_applied(self):
        for epoch in (10, 1_000_000_000):
            status, body = self.post({"records": [_Base.remove("source", "member", epoch)]})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["status"], "synced")
            self.assertEqual(self.roster(), {"member", "source"})
        self.assertTrue(_local_peer_cert_cache_path(self.victim, "source").is_file())

    def test_http_forged_removal_of_the_receiver_is_not_applied(self):
        status, _body = self.post({"records": [_Base.remove("victim", "member", 10)]})
        self.assertEqual(status, 200)
        self.assertEqual(self.roster(), {"member", "source"})
        self.assertFalse(swarm_membership._departed(self.victim))

    def test_http_unpaired_introducer_cannot_remove_anyone(self):
        status, _body = self.post({"records": [_Base.remove("source", "stranger", 10)]}, introducer="stranger")
        self.assertEqual(status, 403)
        self.assertEqual(self.roster(), {"member", "source"})

    def test_http_malformed_json_epochs_do_not_remove(self):
        for epoch in (10.0, "10", None, True, [10], {"v": 10}, -1, 2 ** 70):
            record = _Base.remove("source", "member")
            record["epoch"] = epoch
            status, _body = self.post({"records": [record]})
            self.assertEqual(status, 200, epoch)
            self.assertEqual(self.roster(), {"member", "source"}, epoch)

    def test_http_self_removal_by_authenticated_member_still_works(self):
        status, _body = self.post({"records": [_Base.remove("member", "member", 10)]})
        self.assertEqual(status, 200)
        self.assertEqual(self.roster(), {"source"})


if __name__ == "__main__":
    unittest.main()
