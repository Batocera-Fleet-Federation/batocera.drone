"""Swarm membership propagation (#107).

An approved pairing is shared with Drones that are already trusted. Discovery
does not grant access, a certificate pin is not replaced by gossip, and a
operator removal is local, while authenticated self-departure removes the caller.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.common.auth import DroneCredentialStore, SessionAuth, SessionStore
from app.common.http_cache import ExpiringKeyCache, ExpiringLRUCache
from app.common.settings import Settings
from app.drone_api import DroneThreadingHTTPServer, RomRepository, _build_handler
from app.storage.state_store import database_path
from app.transfer import local_network, swarm_membership
from app.transfer.drone_network import _certificate_pem_fingerprint
from app.transfer.drone_tls import DroneCertificateManager
from app.transfer.peer_connectivity import _local_pair_peer, _save_local_peer_certificate
from app.web.handlers_peer import HandlersPeerMixin


def _roster(settings: Settings) -> set[str]:
    return {str(peer.get("drone_id") or "") for peer in local_network.paired_peers(settings)}


def _reserve_port() -> int:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


class SwarmMembershipTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required to mint Drone certificates")
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            os.environ,
            {
                "HTTP_ONLY": "1",
                "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1",
                "DRONE_COMPAT_HTTPS_PORTS": "",
                "ROM_METADATA_POLL_SECONDS": "0",
                "DRONE_CAST_ENABLED": "0",
                "DRONE_HTTP_REDIRECT_PORT": "0",
                "USE_FAKE_DATA": "0",
            },
            clear=True,
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: swarm_membership.set_membership_poster(None))

    def _settings(self, device_id: str, port: int = 443) -> Settings:
        root = Path(self._tmp.name) / device_id
        for name in ("roms", "bios", "saves", "movies"):
            (root / name).mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(
            os.environ,
            {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "MOVIES_ROOT": str(root / "movies"),
                "DRONE_DEVICE_ID": device_id,
                "HTTPS_PORT": str(port),
                "DRONE_ADVERTISED_API_PORT": str(port),
                "DRONE_PEER_MTLS_PORT": str(port),
                "DRONE_ADVERTISED_PEER_MTLS_PORT": str(port),
                "LOG_DIR": str(root / "logs"),
            },
            clear=False,
        ):
            settings = Settings.from_env()
        DroneCertificateManager(settings).ensure_certificate()
        return settings

    def _approve(self, settings: Settings, other: Settings) -> None:
        certificate = DroneCertificateManager(other).ensure_certificate()
        pem = str(certificate.get("public_certificate") or "")
        cert_path, fingerprint = _save_local_peer_certificate(settings, other.device_id, pem)
        stored = local_network.save_paired_peer(
            settings,
            {
                "drone_id": other.device_id,
                "name": other.device_id,
                "hostname": other.device_id,
                "reachable_url": f"https://127.0.0.1/{other.device_id}",
                "scheme": "https",
                "api_port": 443,
                "peer_mtls_port": 443,
                "pairing_source": "local_network",
                "certificate_fingerprint": fingerprint,
                "certificate_path": str(cert_path),
                "certificate_pem": pem,
            },
        )
        swarm_membership.record_approved_member(settings, stored)

    def _install_poster(self, drones: dict[str, Settings]) -> None:
        def poster(settings: Settings, peer: dict, payload: dict) -> dict:
            target_id = str(peer.get("drone_id") or "")
            target = drones.get(target_id)
            if target is None:
                raise ConnectionError(f"no drone named {target_id}")
            return swarm_membership.handle_membership_request(target, payload, settings.device_id)

        swarm_membership.set_membership_poster(poster)

    def test_join_through_non_central_member_fills_every_roster(self) -> None:
        drones = {name: self._settings(name) for name in ("drone-a", "drone-b", "drone-c")}
        # A is already paired with B. C is approved only by B, the non-central member.
        self._approve(drones["drone-a"], drones["drone-b"])
        self._approve(drones["drone-b"], drones["drone-a"])
        self._approve(drones["drone-b"], drones["drone-c"])
        self._approve(drones["drone-c"], drones["drone-b"])
        self.assertEqual(_roster(drones["drone-a"]), {"drone-b"})
        self.assertNotIn("drone-c", _roster(drones["drone-a"]))

        self._install_poster(drones)
        swarm_membership.propagate_membership(drones["drone-c"])

        self.assertEqual(_roster(drones["drone-a"]), {"drone-b", "drone-c"})
        self.assertEqual(_roster(drones["drone-b"]), {"drone-a", "drone-c"})
        self.assertEqual(_roster(drones["drone-c"]), {"drone-a", "drone-b"})
        introduced = local_network.get_paired_peer(drones["drone-a"], "drone-c")
        self.assertIsNotNone(introduced)
        self.assertEqual(introduced["pairing_source"], "swarm")
        direct = local_network.get_paired_peer(drones["drone-c"], "drone-b")
        self.assertEqual(direct["pairing_source"], "local_network")

    def test_discovery_does_not_grant_membership(self) -> None:
        local = self._settings("drone-a")
        discovered = local_network.record_discovered_peer(
            local,
            {
                "service": local_network.DISCOVERY_SERVICE,
                "drone_id": "drone-stranger",
                "name": "Stranger",
                "scheme": "https",
                "api_port": 443,
                "reachable_url": "https://127.0.0.1:9",
                "certificate_fingerprint": "abc",
            },
            "192.0.2.8",
        )
        self.assertIsNotNone(discovered)
        self.assertEqual(_roster(local), set())
        with self.assertRaises(swarm_membership.MembershipRejected) as raised:
            swarm_membership.handle_membership_request(
                local,
                {
                    "records": [
                        {
                            "peer_id": "drone-stranger",
                            "op": "add",
                            "epoch": 1,
                            "origin": "drone-stranger",
                            "card": {"reachable_url": "https://127.0.0.1:9", "scheme": "https"},
                        }
                    ]
                },
                "drone-stranger",
            )
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(_roster(local), set())

    def test_bad_certificate_and_fingerprint_mismatch_are_not_stored(self) -> None:
        drones = {name: self._settings(name) for name in ("drone-a", "drone-b", "drone-c")}
        self._approve(drones["drone-a"], drones["drone-b"])
        pem = str(DroneCertificateManager(drones["drone-c"]).ensure_certificate().get("public_certificate") or "")
        real_fingerprint = _certificate_pem_fingerprint(pem)
        for card in (
            {
                "reachable_url": "https://127.0.0.1/drone-c",
                "scheme": "https",
                "certificate_pem": "-----BEGIN CERTIFICATE-----\nnot-a-certificate\n-----END CERTIFICATE-----",
                "certificate_fingerprint": "ff",
            },
            {
                "reachable_url": "https://127.0.0.1/drone-c",
                "scheme": "https",
                "certificate_pem": pem,
                "certificate_fingerprint": "0" * 64,
            },
        ):
            swarm_membership.handle_membership_request(
                drones["drone-a"],
                {
                    "records": [
                        {
                            "peer_id": "drone-c",
                            "op": "add",
                            "epoch": 5,
                            "origin": "drone-b",
                            "card": card,
                        }
                    ]
                },
                "drone-b",
            )
            self.assertNotIn("drone-c", _roster(drones["drone-a"]))
        self.assertNotEqual(real_fingerprint, "0" * 64)

    def test_zero_length_certificate_card_is_not_pinned_by_gossip(self) -> None:
        # #424: a body that base64-decodes to zero bytes must not hash to the empty-input
        # digest and be pinned, even when the card claims that digest itself.
        drones = {name: self._settings(name) for name in ("drone-a", "drone-b", "drone-c")}
        self._approve(drones["drone-a"], drones["drone-b"])
        empty_digest = hashlib.sha256(b"").hexdigest()
        card = {
            "reachable_url": "https://127.0.0.1/drone-c",
            "scheme": "https",
            "certificate_pem": "-----BEGIN CERTIFICATE-----\n!!!!\n-----END CERTIFICATE-----\n",
            "certificate_fingerprint": empty_digest,
        }
        with mock.patch("app.transfer.peer_connectivity._save_local_peer_certificate") as save_cert:
            swarm_membership.handle_membership_request(
                drones["drone-a"],
                {"records": [{"peer_id": "drone-c", "op": "add", "epoch": 5, "origin": "drone-b", "card": card}]},
                "drone-b",
            )
        save_cert.assert_not_called()
        self.assertNotIn("drone-c", _roster(drones["drone-a"]))
        self.assertNotIn(
            empty_digest,
            {str(peer.get("certificate_fingerprint") or "") for peer in local_network.paired_peers(drones["drone-a"])},
        )

    def test_gossip_cannot_replace_a_pinned_fingerprint(self) -> None:
        drones = {name: self._settings(name) for name in ("drone-a", "drone-b", "drone-c", "drone-d")}
        self._approve(drones["drone-a"], drones["drone-b"])
        self._approve(drones["drone-a"], drones["drone-c"])
        pinned = local_network.get_paired_peer(drones["drone-a"], "drone-c")["certificate_fingerprint"]
        replacement = str(DroneCertificateManager(drones["drone-d"]).ensure_certificate().get("public_certificate") or "")
        swarm_membership.handle_membership_request(
            drones["drone-a"],
            {
                "records": [
                    {
                        "peer_id": "drone-c",
                        "op": "add",
                        "epoch": 1_000_000,
                        "origin": "drone-b",
                        "card": {
                            "name": "replaced",
                            "reachable_url": "https://127.0.0.1/drone-c",
                            "scheme": "https",
                            "certificate_pem": replacement,
                            "certificate_fingerprint": _certificate_pem_fingerprint(replacement),
                        },
                    }
                ]
            },
            "drone-b",
        )
        current = local_network.get_paired_peer(drones["drone-a"], "drone-c")
        self.assertEqual(current["certificate_fingerprint"], pinned)
        self.assertNotEqual(current.get("name"), "replaced")

    def test_rejected_gossip_routes_preserve_live_certificate_and_roster(self) -> None:
        drones = {name: self._settings(name) for name in ("drone-a", "drone-b", "drone-c")}
        local = drones["drone-a"]
        self._approve(local, drones["drone-b"])
        self._approve(local, drones["drone-c"])
        original = local_network.get_paired_peer(local, "drone-c")
        certificate = Path(original["certificate_path"])
        original_bytes = certificate.read_bytes()
        for overrides in (
            {"scheme": "http", "reachable_url": "http://attacker.local"},
            {"scheme": "ftp", "reachable_url": "ftp://attacker.local"},
            {"scheme": "https", "reachable_url": ""},
            {"scheme": "https", "reachable_url": "http://attacker.local"},
            {"scheme": "https", "advertised_reachable_url": "http://attacker.local"},
            {"scheme": "https", "reachable_url": "https://peer.local:invalid"},
        ):
            with self.subTest(overrides=overrides):
                card = {"scheme": "https", "reachable_url": "https://peer.local",
                        "certificate_pem": original_bytes.decode(),
                        "certificate_fingerprint": original["certificate_fingerprint"], **overrides}
                swarm_membership.handle_membership_request(local, {
                    "hop": 3, "records": [{"peer_id": "drone-c", "op": "add", "epoch": 1_000_000,
                                            "origin": "drone-b", "card": card}],
                }, "drone-b")
                self.assertEqual(local_network.get_paired_peer(local, "drone-c"), original)
                self.assertEqual(certificate.read_bytes(), original_bytes)
                exported = swarm_membership.membership_view(local)
                records = exported["records"]
                self.assertFalse(any(record["epoch"] == 1_000_000 for record in records))

    def test_stale_add_does_not_beat_a_removal(self) -> None:
        drones = {name: self._settings(name) for name in ("drone-a", "drone-b", "drone-c")}
        self._approve(drones["drone-a"], drones["drone-b"])
        self._approve(drones["drone-a"], drones["drone-c"])
        self.assertTrue(local_network.forget_peer(drones["drone-a"], "drone-c"))
        certificate = DroneCertificateManager(drones["drone-c"]).ensure_certificate()
        pem = str(certificate.get("public_certificate") or "")
        swarm_membership.handle_membership_request(
            drones["drone-a"],
            {
                "records": [
                    {
                        "peer_id": "drone-c",
                        "op": "add",
                        "epoch": 1,
                        "origin": "drone-b",
                        "card": {
                            "reachable_url": "https://127.0.0.1/drone-c",
                            "scheme": "https",
                            "certificate_pem": pem,
                            "certificate_fingerprint": _certificate_pem_fingerprint(pem),
                        },
                    }
                ]
            },
            "drone-b",
        )
        self.assertNotIn("drone-c", _roster(drones["drone-a"]))

    def test_explicit_repair_after_removal_rejoins_and_propagates(self) -> None:
        drones = {name: self._settings(name) for name in ("drone-a", "drone-b", "drone-c")}
        for left, right in (("drone-a", "drone-b"), ("drone-b", "drone-c")):
            self._approve(drones[left], drones[right])
            self._approve(drones[right], drones[left])
        self._install_poster(drones)
        swarm_membership.propagate_membership(drones["drone-c"])
        self.assertTrue(swarm_membership.publish_forget(drones["drone-a"], "drone-c"))
        self.assertNotIn("drone-c", _roster(drones["drone-a"]))
        self.assertEqual(_roster(drones["drone-b"]), {"drone-a", "drone-c"})
        self.assertEqual(_roster(drones["drone-c"]), {"drone-a", "drone-b"})

        # Rejoin through the operator who forgot C; another roster's approval
        # is not a replacement for this fresh local pairing approval.
        self._approve(drones["drone-a"], drones["drone-c"])
        self._approve(drones["drone-c"], drones["drone-a"])
        swarm_membership.propagate_membership(drones["drone-a"])
        self.assertEqual(_roster(drones["drone-a"]), {"drone-b", "drone-c"})
        self.assertEqual(_roster(drones["drone-b"]), {"drone-a", "drone-c"})
        self.assertEqual(_roster(drones["drone-c"]), {"drone-a", "drone-b"})

    def test_immediate_repair_after_forget_keeps_peer_and_accepts_its_first_post(self) -> None:
        drones = {name: self._settings(name) for name in ("victim", "member", "source")}
        self._approve(drones["victim"], drones["source"])
        self._approve(drones["victim"], drones["member"])
        self._approve(drones["member"], drones["victim"])
        self._install_poster(drones)
        self.assertTrue(swarm_membership.publish_forget(drones["victim"], "member"))
        self.assertNotIn("member", _roster(drones["victim"]))

        # Same call order as the pairing handlers: save the pin, then record the approval.
        self._approve(drones["victim"], drones["member"])
        self.assertIn("member", _roster(drones["victim"]))

        # The approved peer's first membership POST is not refused with 403.
        view = swarm_membership.handle_membership_request(drones["victim"], {"records": []}, "member")
        self.assertEqual(view.get("status"), "synced")
        self.assertIn("member", _roster(drones["victim"]))

    def test_operator_removal_only_changes_its_own_roster(self) -> None:
        drones = {name: self._settings(name) for name in ("drone-a", "drone-b", "drone-c")}
        for left, right in (("drone-a", "drone-b"), ("drone-b", "drone-c")):
            self._approve(drones[left], drones[right])
            self._approve(drones[right], drones[left])
        self._install_poster(drones)
        swarm_membership.propagate_membership(drones["drone-c"])
        self.assertTrue(swarm_membership.publish_forget(drones["drone-b"], "drone-c"))

        self.assertEqual(_roster(drones["drone-a"]), {"drone-b", "drone-c"})
        self.assertEqual(_roster(drones["drone-b"]), {"drone-a"})
        self.assertEqual(_roster(drones["drone-c"]), {"drone-a", "drone-b"})
        removed = swarm_membership.handle_membership_request(
            drones["drone-b"],
            {"records": []},
            "drone-c",
        )
        self.assertEqual(removed["status"], "removed")
        self.assertEqual([record["peer_id"] for record in removed["records"]], ["drone-c"])
        self.assertEqual(_roster(drones["drone-a"]), {"drone-b", "drone-c"})

    def _remove_record(self, peer_id: str, origin: str, epoch: int = 5) -> dict:
        return {"peer_id": peer_id, "op": "remove", "epoch": epoch, "origin": origin, "card": {}}

    def _mesh(self, names: tuple[str, ...]) -> dict[str, Settings]:
        drones = {name: self._settings(name) for name in names}
        for left in names:
            for right in names:
                if left != right:
                    self._approve(drones[left], drones[right])
        return drones

    def test_member_cannot_relay_a_removal_attributed_to_someone_else(self) -> None:
        drones = self._mesh(("drone-v", "drone-m", "drone-s"))
        for origin in ("drone-s", "drone-x", ""):
            swarm_membership.handle_membership_request(
                drones["drone-v"],
                {"records": [self._remove_record("drone-s", origin)]},
                "drone-m",
            )
            self.assertEqual(_roster(drones["drone-v"]), {"drone-m", "drone-s"})
        self.assertFalse(
            any(
                record["peer_id"] == "drone-s" and record["op"] == "remove"
                for record in swarm_membership.membership_view(drones["drone-v"])["records"]
            )
        )

    def test_removal_with_forged_far_future_epoch_is_ignored(self) -> None:
        drones = self._mesh(("drone-v", "drone-m", "drone-s"))
        swarm_membership.handle_membership_request(
            drones["drone-v"],
            {"records": [self._remove_record("drone-s", "drone-m", 1_000_000_000)]},
            "drone-m",
        )
        self.assertIn("drone-s", _roster(drones["drone-v"]))

    def test_member_cannot_force_departure_with_relayed_removal(self) -> None:
        drones = self._mesh(("drone-v", "drone-m"))
        swarm_membership.handle_membership_request(
            drones["drone-v"],
            {"records": [self._remove_record("drone-v", "drone-x")]},
            "drone-m",
        )
        self.assertEqual(_roster(drones["drone-v"]), {"drone-m"})

    def test_introducer_origin_does_not_authorize_removal_of_a_peer(self) -> None:
        drones = self._mesh(("drone-v", "drone-m", "drone-s"))
        swarm_membership.handle_membership_request(
            drones["drone-v"],
            {"records": [self._remove_record("drone-s", "drone-m", 1_000)]},
            "drone-m",
        )
        self.assertEqual(_roster(drones["drone-v"]), {"drone-m", "drone-s"})

    def test_operator_removal_stays_local_in_a_full_mesh(self) -> None:
        drones = self._mesh(("drone-a", "drone-b", "drone-c", "drone-d"))
        self._install_poster(drones)
        self.assertTrue(swarm_membership.publish_forget(drones["drone-b"], "drone-d"))
        self.assertNotIn("drone-d", _roster(drones["drone-b"]))
        for name in ("drone-a", "drone-c"):
            self.assertIn("drone-d", _roster(drones[name]))
        self.assertEqual(_roster(drones["drone-d"]), {"drone-a", "drone-b", "drone-c"})

    def test_tombstones_are_retained_locally_but_not_relayed_in_requests_or_replies(self) -> None:
        drones = self._mesh(("drone-v", "drone-m", "drone-s"))
        victim = drones["drone-v"]
        sent = []

        def poster(settings, peer, payload):
            sent.append((peer["drone_id"], payload))
            return {"records": []}

        swarm_membership.set_membership_poster(poster)
        response = swarm_membership.handle_membership_request(
            victim, {"records": [self._remove_record("drone-m", "drone-m")]}, "drone-m",
        )
        self.assertEqual(_roster(victim), {"drone-s"})
        self.assertTrue(any(r["op"] == "remove" for r in swarm_membership.membership_view(victim)["records"]))
        self.assertTrue(sent)
        self.assertTrue(all(peer_id == "drone-s" for peer_id, _ in sent))
        for payload in [response] + [payload for _, payload in sent]:
            self.assertFalse(any(r["op"] == "remove" for r in payload["records"]))

        sent.clear()
        self.assertTrue(swarm_membership.publish_forget(victim, "drone-s"))
        self.assertEqual(sent, [], "operator forget must not contact the forgotten peer")
        response = swarm_membership.handle_membership_request(victim, {"records": []}, "drone-s")
        self.assertEqual(response["status"], "removed")

    def test_sync_reply_self_departure_unlinks_pin_and_does_not_relay_removal(self) -> None:
        drones = self._mesh(("drone-v", "drone-m", "drone-s"))
        victim = drones["drone-v"]
        cert_path = Path(local_network.get_paired_peer(victim, "drone-m")["certificate_path"])
        sent = []

        def poster(settings, peer, payload):
            sent.append(payload)
            return {"records": [self._remove_record("drone-m", "drone-m")]} if peer["drone_id"] == "drone-m" else {"records": []}

        swarm_membership.set_membership_poster(poster)
        swarm_membership.propagate_membership(victim)
        self.assertEqual(_roster(victim), {"drone-s"})
        self.assertFalse(cert_path.exists())
        self.assertTrue(all(r["op"] != "remove" for payload in sent for r in payload["records"]))

    def test_removed_reply_cannot_stop_sync_to_other_members(self) -> None:
        drones = self._mesh(("drone-v", "drone-m", "drone-s"))
        victim = drones["drone-v"]
        contacted = []

        def poster(settings, peer, payload):
            contacted.append(peer["drone_id"])
            return {"status": "removed", "records": [self._remove_record("drone-v", peer["drone_id"])]}

        swarm_membership.set_membership_poster(poster)
        swarm_membership.propagate_membership(victim)
        self.assertEqual(set(contacted), {"drone-m", "drone-s"})
        self.assertEqual(_roster(victim), {"drone-m", "drone-s"})
        self.assertTrue(swarm_membership._is_dirty(victim))

    def test_removed_caller_is_recognized_by_pinned_fingerprint(self) -> None:
        local = self._settings("drone-a")
        other = self._settings("drone-c")
        self._approve(local, other)
        fingerprint = local_network.get_paired_peer(local, "drone-c")["certificate_fingerprint"]
        self.assertTrue(local_network.forget_peer(local, "drone-c"))
        os.environ.pop("DRONE_LOCAL_ALLOW_INSECURE_HTTP", None)
        stub = HandlersPeerMixin()
        stub.settings = local
        stub._peer_requester_device_id = lambda: ""
        stub._presented_client_fingerprint = lambda: fingerprint
        self.assertEqual(stub._membership_caller_id({}), "drone-c")
        stub._presented_client_fingerprint = lambda: ""
        self.assertEqual(stub._membership_caller_id({"introducer_id": "drone-c"}), "")

    def test_insecure_introducer_requires_http_test_mode(self) -> None:
        local = self._settings("drone-a")
        stub = HandlersPeerMixin()
        stub.settings = local
        stub._peer_requester_device_id = lambda: ""
        stub._presented_client_fingerprint = lambda: ""
        os.environ["DRONE_LOCAL_ALLOW_INSECURE_HTTP"] = "1"
        self.assertEqual(stub._membership_caller_id({"introducer_id": "drone-b"}), "drone-b")
        root = Path(self._tmp.name) / "drone-https"
        root.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(
            os.environ,
            {"HTTP_ONLY": "0", "USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "drone-https"},
            clear=False,
        ):
            https = Settings.from_env()
        stub.settings = https
        self.assertFalse(https.http_only)
        self.assertEqual(stub._membership_caller_id({"introducer_id": "drone-b"}), "")


class SwarmMembershipHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required to mint Drone certificates")
        self._tmp = tempfile.TemporaryDirectory()
        self._servers: list[DroneThreadingHTTPServer] = []
        self._env = mock.patch.dict(
            os.environ,
            {
                "HTTP_ONLY": "1",
                "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1",
                "DRONE_COMPAT_HTTPS_PORTS": "",
                "ROM_METADATA_POLL_SECONDS": "0",
                "DRONE_CAST_ENABLED": "0",
                "DRONE_HTTP_REDIRECT_PORT": "0",
                "USE_FAKE_DATA": "0",
            },
            clear=True,
        )
        self._env.start()
        real_discovery = local_network.discovery_payload

        def loopback_discovery(settings, certificate_fingerprint=""):
            payload = real_discovery(settings, certificate_fingerprint)
            port = int(settings.advertised_api_port or settings.https_port)
            payload["reachable_url"] = f"http://127.0.0.1:{port}"
            payload["tailnet_ip"] = ""
            return payload

        self._discovery = mock.patch.object(local_network, "discovery_payload", loopback_discovery)
        self._tailnet = mock.patch.object(local_network, "get_tailnet_ip", return_value=None)
        self._discovery.start()
        self._tailnet.start()
        swarm_membership.set_membership_poster(None)
        self.addCleanup(self._stop)
        self.addCleanup(self._tailnet.stop)
        self.addCleanup(self._discovery.stop)
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: swarm_membership.set_membership_poster(None))

    def _stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()

    def _start(self, device_id: str) -> Settings:
        port = _reserve_port()
        root = Path(self._tmp.name) / device_id
        for name in ("roms", "bios", "saves", "movies"):
            (root / name).mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(
            os.environ,
            {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "MOVIES_ROOT": str(root / "movies"),
                "DRONE_DEVICE_ID": device_id,
                "HTTPS_PORT": str(port),
                "DRONE_ADVERTISED_API_PORT": str(port),
                "DRONE_PEER_MTLS_PORT": str(port),
                "DRONE_ADVERTISED_PEER_MTLS_PORT": str(port),
                "LOG_DIR": str(root / "logs"),
            },
            clear=False,
        ):
            settings = Settings.from_env()
        DroneCertificateManager(settings).ensure_certificate()
        database = database_path(settings.userdata_root)
        auth = SessionAuth(
            DroneCredentialStore(settings.credentials_file, state_database_file=database),
            SessionStore(database),
        )
        repository = RomRepository(settings.roms_root, settings.bios_root, settings=settings)
        handler = _build_handler(
            settings,
            auth,
            repository,
            ExpiringLRUCache(60, 8, 1024),
            ExpiringKeyCache(60),
            ExpiringLRUCache(60, 8, 1024),
        )
        server = DroneThreadingHTTPServer(("127.0.0.1", port), handler)
        thread = threading.Thread(target=server.serve_forever, name=f"swarm-{device_id}", daemon=True)
        thread.start()
        self._servers.append(server)
        return settings

    def _pair(self, initiator: Settings, target: Settings) -> None:
        code = str(local_network.pairing_code(target)["code"])
        certificate = DroneCertificateManager(target).ensure_certificate()
        _local_pair_peer(
            initiator,
            {
                "drone_id": target.device_id,
                "name": target.device_id,
                "reachable_url": f"http://127.0.0.1:{target.https_port}",
                "scheme": "http",
                "api_port": target.https_port,
                "peer_mtls_port": target.https_port,
                "certificate_fingerprint": str(certificate.get("fingerprint") or ""),
                "pairing_source": "local_network",
            },
            code,
            confirmed_fingerprint=str(certificate.get("fingerprint") or ""),
        )

    def test_http_join_through_non_central_member_and_removal(self) -> None:
        drone_a = self._start("drone-a")
        drone_b = self._start("drone-b")
        drone_c = self._start("drone-c")
        self._pair(drone_a, drone_b)
        self.assertEqual(_roster(drone_a), {"drone-b"})
        self.assertEqual(_roster(drone_b), {"drone-a"})
        self._pair(drone_c, drone_b)

        self.assertEqual(_roster(drone_a), {"drone-b", "drone-c"})
        self.assertEqual(_roster(drone_b), {"drone-a", "drone-c"})
        self.assertEqual(_roster(drone_c), {"drone-a", "drone-b"})

        self.assertTrue(swarm_membership.publish_forget(drone_b, "drone-c"))
        self.assertEqual(_roster(drone_a), {"drone-b", "drone-c"})
        self.assertEqual(_roster(drone_b), {"drone-a"})
        self.assertEqual(_roster(drone_c), {"drone-a", "drone-b"})


if __name__ == "__main__":
    unittest.main()
