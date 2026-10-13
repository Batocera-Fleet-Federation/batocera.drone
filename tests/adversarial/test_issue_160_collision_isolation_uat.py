"""Issue #160: duplicate-identity handling must isolate, never reset or revoke.

Requirement 12: if two endpoints claim the same identity, do not silently
accept both, do not overwrite pinned certificates, do not automatically
revoke the original, and do not let an untrusted peer force this Drone to
reset its identity. Peer-supplied hardware claims are untrusted; possession
of a copied private key is not proof that the copy is a different machine.
"""

import hashlib
import json
import os
import shutil
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.storage.state_store import database_path, load_events, load_payload, save_payload
from app.transfer import local_network, swarm_membership
from app.transfer.drone_network import _certificate_pem_fingerprint


PEER_ID = "11:22:33:44:55:66"
PINNED_FP = "ab" * 32


def hardware(root: Path, uuid: str, serial: str) -> HardwareIdentityProvider:
    directory = root / "sys/class/dmi/id"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "product_uuid").write_text(uuid + "\n", encoding="utf-8")
    (directory / "board_serial").write_text(serial + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def settings_for(root: Path) -> Settings:
    with mock.patch.dict(os.environ, {
        "USERDATA_ROOT": str(root),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
    }, clear=True):
        return Settings.from_env()


def fingerprint(item: Settings) -> str:
    pem = item.drone_cert_file.read_text(encoding="utf-8")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class CollisionIsolationUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-160-collision-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hw = hardware(self.tmp / "hw", "3f713284-3270-4c5f-990c-17231045b68e", "BOARD-ORIGINAL-8941")
        self.settings = settings_for(self.tmp / "original")
        self.assertEqual(identity.ensure_identity(self.settings, self.hw).state, identity.State.READY)
        save_payload(
            database_path(self.settings.userdata_root),
            "local_paired_peers",
            {PEER_ID: {"drone_id": PEER_ID, "name": "friend", "certificate_fingerprint": PINNED_FP, "paired": True}},
        )
        self.snapshot = {
            "cert": self.settings.drone_cert_file.read_bytes(),
            "key": self.settings.drone_key_file.read_bytes(),
            "device_id": self.settings.device_id,
            "binding": identity._binding_path(self.settings).read_bytes(),
            "peers": load_payload(database_path(self.settings.userdata_root), "local_paired_peers", {}),
        }

    def assert_identity_untouched(self, label: str) -> None:
        self.assertEqual(self.settings.drone_cert_file.read_bytes(), self.snapshot["cert"], msg=label)
        self.assertEqual(self.settings.drone_key_file.read_bytes(), self.snapshot["key"], msg=label)
        self.assertEqual(self.settings.device_id, self.snapshot["device_id"], msg=label)
        self.assertEqual(identity._binding_path(self.settings).read_bytes(), self.snapshot["binding"], msg=label)
        self.assertEqual(
            load_payload(database_path(self.settings.userdata_root), "local_paired_peers", {}),
            self.snapshot["peers"],
            msg=label,
        )
        self.assertTrue(identity.swarm_networking_allowed(), msg=label)

    def test_duplicate_claims_are_isolated_and_cannot_reset_this_identity(self) -> None:
        own_pem = self.settings.drone_cert_file.read_text(encoding="utf-8")
        own_fp = fingerprint(self.settings)

        identity.record_collision(self.settings, "peer-request-own-certificate", "copied key presented")
        identity.record_collision(self.settings, "pair-request-own-device-id")
        identity.record_collision(self.settings, "hardware-claim-from-peer", json.dumps({
            "dmi_product_uuid": "definitely-different-hardware",
            "platform_serial": "SPOOFED-SERIAL-9999",
        }))

        gossip = {
            "peer_id": "ca:fe:ca:fe:00:09",
            "op": "add",
            "epoch": 99,
            "origin": "untrusted",
            "card": {
                "certificate_pem": own_pem,
                "certificate_fingerprint": own_fp,
                "reachable_url": "https://10.0.0.77",
                "scheme": "https",
            },
        }
        self.assertFalse(swarm_membership._apply_add(self.settings, {"clock": 0, "records": {}}, gossip))

        discovered = local_network.record_discovered_peer(
            self.settings,
            {
                "service": local_network.DISCOVERY_SERVICE,
                "drone_id": self.settings.device_id,
                "name": "copy",
                "certificate_fingerprint": own_fp,
                "scheme": "https",
                "api_port": 443,
            },
            source_ip="10.0.0.77",
        )
        self.assertTrue(discovered["identity_conflict"])
        self.assertFalse(discovered["paired"])

        replacement = {
            "peer_id": PEER_ID,
            "op": "add",
            "epoch": 100,
            "origin": "untrusted",
            "card": {
                "certificate_pem": own_pem,
                "certificate_fingerprint": own_fp,
                "reachable_url": "https://10.0.0.77",
                "scheme": "https",
            },
        }
        self.assertFalse(swarm_membership._apply_add(self.settings, {"clock": 0, "records": {}}, replacement))
        pinned = local_network.get_paired_peer(self.settings, PEER_ID)
        self.assertEqual(pinned["certificate_fingerprint"], PINNED_FP)

        identity.reset_runtime_state_for_tests()
        again = identity.ensure_identity(settings_for(self.settings.userdata_root), self.hw)
        self.assertEqual(again.status, identity.STATUS_VERIFIED)
        self.assert_identity_untouched("after collisions and reboot")

        events = load_events(database_path(self.settings.userdata_root), "identity_collisions")
        sources = {event.get("source") for event in events}
        self.assertIn("peer-request-own-certificate", sources)
        self.assertIn("membership-own-certificate", sources)
        self.assertIn("discovery-duplicate-identity", sources)
        for event in events:
            self.assertNotIn("PRIVATE KEY", json.dumps(event))
            self.assertNotIn(own_pem.strip().splitlines()[1], json.dumps(event))

    def test_peer_fingerprint_helper_does_not_treat_copied_key_material_as_a_new_peer(self) -> None:
        own_pem = self.settings.drone_cert_file.read_text(encoding="utf-8")
        self.assertEqual(_certificate_pem_fingerprint(own_pem).lower(), fingerprint(self.settings))
        self.assert_identity_untouched("fingerprint helper")


if __name__ == "__main__":
    unittest.main()
