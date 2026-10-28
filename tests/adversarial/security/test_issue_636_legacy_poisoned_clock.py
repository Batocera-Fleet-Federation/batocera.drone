"""A persisted clock poisoned before the bound must not silence membership gossip."""

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


class LegacyPoisonedClockTests(unittest.TestCase):
    def test_preexisting_ceiling_clock_can_publish_a_new_approved_member(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl required")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
            "HTTP_ONLY": "1", "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1",
            "DRONE_COMPAT_HTTPS_PORTS": "", "DRONE_CAST_ENABLED": "0",
            "DRONE_HTTP_REDIRECT_PORT": "0", "USE_FAKE_DATA": "0",
        }, clear=True):
            def settings(device_id):
                root = Path(tmp) / device_id
                for name in ("roms", "bios", "saves", "movies"):
                    (root / name).mkdir(parents=True, exist_ok=True)
                with mock.patch.dict(os.environ, {
                    "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
                    "MOVIES_ROOT": str(root / "movies"), "DRONE_DEVICE_ID": device_id,
                    "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite"),
                }, clear=False):
                    result = Settings.from_env()
                DroneCertificateManager(result).ensure_certificate()
                return result

            def approve(receiver, other):
                cert = DroneCertificateManager(other).ensure_certificate()["public_certificate"]
                cert_path, fingerprint = _save_local_peer_certificate(receiver, other.device_id, cert)
                return local_network.save_paired_peer(receiver, {
                    "drone_id": other.device_id, "name": other.device_id,
                    "reachable_url": f"https://127.0.0.1/{other.device_id}", "scheme": "https",
                    "api_port": 443, "peer_mtls_port": 443,
                    "certificate_fingerprint": fingerprint, "certificate_path": str(cert_path),
                    "certificate_pem": cert,
                })

            receiver, source, newcomer = (settings(name) for name in ("receiver", "source", "newcomer"))
            approve(receiver, source)
            newcomer_peer = approve(source, newcomer)

            # Model the persistent state left by the pre-fix attack. The
            # current loader clamps this to the same parse ceiling, and minting
            # still emits a record too far ahead for an unpoisoned receiver.
            ceiling = swarm_membership._MAX_EPOCH
            swarm_membership._with_state(source, lambda state: state.update(clock=ceiling))
            swarm_membership.record_approved_member(source, newcomer_peer)
            record = next(
                row for row in swarm_membership.membership_view(source)["records"]
                if row["peer_id"] == "newcomer"
            )
            self.assertEqual(record["epoch"], ceiling)

            swarm_membership.handle_membership_request(
                receiver, {"records": [record]}, "source"
            )
            self.assertIsNotNone(
                local_network.get_paired_peer(receiver, "newcomer"),
                "a Drone with a legacy poisoned clock cannot publish a new approved member",
            )


if __name__ == "__main__":
    unittest.main()
