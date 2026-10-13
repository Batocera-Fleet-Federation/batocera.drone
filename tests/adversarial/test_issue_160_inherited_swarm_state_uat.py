"""Issue #160: a clone must drop identity-bound swarm state and keep user content.

Requirement 7: paired peers, pins, membership (including tombstones), pairing
codes, discovery/health caches, share/NFS authorizations, peer download jobs,
and credentials imported from the old swarm must not survive. Login, ROMs,
automation, torrents, and locally owned SMTP settings that are not a peer
import must survive. Requirement 2C: the clone must not announce a removal.
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
from app.device import smtp_manager, vpn_manager
from app.storage.state_store import database_path, load_payload, save_payload, save_peer_route
from app.transfer import local_network, swarm_membership, swarm_reset


PEER_ID = "aa:bb:cc:dd:ee:01"


def hardware(root: Path, uuid: str, serial: str) -> HardwareIdentityProvider:
    directory = root / "sys/class/dmi/id"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "product_uuid").write_text(uuid + "\n", encoding="utf-8")
    (directory / "board_serial").write_text(serial + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def settings_for(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
        "DRONE_VPN_DIR": str(root / "vpn"),
    }
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings.from_env()


def fingerprint(item: Settings) -> str:
    pem = item.drone_cert_file.read_text(encoding="utf-8")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class InheritedSwarmStateUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-160-inherited-state-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hw_a = hardware(self.tmp / "hw-a", "c25ddfd4-6e36-48cd-b489-1843d4da74ad", "BOARD-ORIGINAL-8291")
        self.hw_b = hardware(self.tmp / "hw-b", "20b5f8b7-5962-4b8e-985a-bc3c6ace160d", "BOARD-CLONE-8172")
        self.original = settings_for(self.tmp / "original")
        self.assertEqual(identity.ensure_identity(self.original, self.hw_a).state, identity.State.READY)
        vpn_dir = mock.patch.object(vpn_manager, "vpn_dir", lambda settings: Path(settings.userdata_root) / "vpn")
        vpn_dir.start()
        self.addCleanup(vpn_dir.stop)
        self._seed()

    def _seed(self) -> None:
        db = database_path(self.original.userdata_root)
        peer = {
            "drone_id": PEER_ID,
            "device_id": PEER_ID,
            "name": "peer",
            "certificate_fingerprint": "ab" * 32,
            "paired": True,
        }
        save_payload(db, "local_paired_peers", {PEER_ID: peer})
        save_payload(db, "local_discovered_peers", {PEER_ID: peer, "identity-conflict:10.0.0.9": dict(peer)})
        save_payload(db, "local_pairing_code", {"code": "87654321", "expires_at": "2999-01-01T00:00:00+00:00"})
        save_payload(db, "local_peer_checks", [{"peer": PEER_ID}])
        save_payload(db, "tailnet_forgotten_peers", [PEER_ID])
        save_payload(
            db,
            "swarm_membership",
            {
                "clock": 9,
                "departed": True,
                "records": {
                    PEER_ID: {"op": "remove", "epoch": 8, "origin": PEER_ID, "card": {}},
                    "ff:ee:dd:cc:bb:aa": {"op": "add", "epoch": 2, "origin": PEER_ID, "card": {}},
                },
            },
        )
        save_payload(db, "network_share_manager.json", {"peers": {PEER_ID: {"mounted": True, "export": "/roms"}}})
        save_payload(db, "nfs_export_manager.json", {"peers": {PEER_ID: {"allowed": True}}})
        save_payload(db, "network_reference_selection.json", {"peer": PEER_ID, "library": "roms"})
        save_payload(
            db,
            "download_manager.json",
            {
                "version": 1,
                "paused": True,
                "jobs": [
                    {"job_id": "peer-job", "source_drone_id": PEER_ID},
                    {"job_id": "local-job", "source_drone_id": ""},
                ],
            },
        )
        save_payload(db, "torrent_manager.json", {"items": [{"id": "keep-this-torrent"}]})
        save_payload(db, "automation_config.json", {"wifi_recovery": {"enabled": True}})
        save_peer_route(db, PEER_ID, "10.0.0.9", "ip")
        pins = self.original.userdata_root / "system" / "drone-app" / "local-peer-certs"
        pins.mkdir(parents=True, exist_ok=True)
        (pins / "peer.crt").write_text("-----BEGIN CERTIFICATE-----\npinned\n", encoding="utf-8")
        rom = self.original.userdata_root / "roms" / "snes"
        rom.mkdir(parents=True)
        (rom / "game.sfc").write_bytes(b"rom-bytes")
        self.original.credentials_file.parent.mkdir(parents=True, exist_ok=True)
        self.original.credentials_file.write_text(
            json.dumps({"username": "admin", "password_hash": "local-login"}),
            encoding="utf-8",
        )
        smtp_manager._save_state(
            self.original,
            has_config=True,
            host="smtp.example.test",
            password="smtp-imported-secret",
            source_peer_id=PEER_ID,
            recipient_email="me@example.test",
        )
        vpn_manager._save_state(
            self.original,
            has_config=True,
            has_credentials=True,
            username="vpnuser",
            source_peer_id=PEER_ID,
            sharing_enabled=False,
        )
        auth = vpn_manager.auth_path(self.original)
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text("vpnuser\nvpn-imported-secret\n", encoding="utf-8")

    def test_clone_clears_identity_bound_state_and_keeps_user_content(self) -> None:
        original_fp = fingerprint(self.original)
        target = self.tmp / "clone"
        shutil.copytree(self.original.userdata_root, target, symlinks=True)

        with mock.patch.object(local_network, "forget_peer") as forget, \
                mock.patch.object(swarm_membership, "publish_forget") as publish, \
                mock.patch.object(swarm_membership, "record_local_removal") as tombstone, \
                mock.patch.object(swarm_membership, "propagate_membership") as propagate, \
                mock.patch("urllib.request.urlopen") as urlopen:
            identity.reset_runtime_state_for_tests()
            clone = settings_for(target)
            result = identity.ensure_identity(clone, self.hw_b)

        self.assertEqual(result.status, identity.STATUS_CLONE)
        self.assertNotEqual(fingerprint(clone), original_fp)
        self.assertNotEqual(result.settings.device_id, self.original.device_id)
        for call in (forget, publish, tombstone, propagate, urlopen):
            call.assert_not_called()

        db = database_path(target)
        for namespace in swarm_reset.IDENTITY_BOUND_NAMESPACES:
            self.assertEqual(load_payload(db, namespace, "missing"), "missing", msg=namespace)
        self.assertEqual(load_payload(db, "local_paired_peers", {}), {})
        self.assertEqual(list((target / "system/drone-app/local-peer-certs").glob("*")), [])
        queue = load_payload(db, "download_manager.json", {})
        self.assertEqual([job["job_id"] for job in queue["jobs"]], ["local-job"])
        self.assertTrue(queue["paused"])
        self.assertEqual(load_payload(db, "torrent_manager.json", {})["items"][0]["id"], "keep-this-torrent")
        self.assertEqual(load_payload(db, "automation_config.json", {}), {"wifi_recovery": {"enabled": True}})
        self.assertEqual(json.loads(clone.credentials_file.read_text())["username"], "admin")
        self.assertEqual((target / "roms/snes/game.sfc").read_bytes(), b"rom-bytes")

        smtp = smtp_manager._load_state(clone)
        self.assertEqual(smtp["password"], "")
        self.assertEqual(smtp["host"], "smtp.example.test")
        vpn = vpn_manager._load_state(clone)
        self.assertFalse(vpn["has_credentials"])
        self.assertFalse(vpn_manager.auth_path(clone).exists())

        self.assertEqual(fingerprint(self.original), original_fp)
        self.assertEqual(load_payload(database_path(self.original.userdata_root), "local_paired_peers", {})[PEER_ID]["drone_id"], PEER_ID)
        self.assertEqual(
            load_payload(database_path(self.original.userdata_root), "swarm_membership", {})["records"][PEER_ID]["op"],
            "remove",
        )
        self.assertTrue((self.original.userdata_root / "system/drone-app/local-peer-certs/peer.crt").exists())


if __name__ == "__main__":
    unittest.main()
