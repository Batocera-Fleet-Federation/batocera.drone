"""A paired member's forged add must not keep a departing member paired.

Issue #636: "A forged high-epoch add also keeps a member's record from being
superseded by any remove with a lower epoch."
"""

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


class ForgedAddBlocksSelfDepartureTests(unittest.TestCase):
    def test_bounded_forged_add_cannot_outrank_members_own_departure(self):
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
                    "HTTPS_PORT": "443", "DRONE_ADVERTISED_API_PORT": "443",
                    "DRONE_PEER_MTLS_PORT": "443", "DRONE_ADVERTISED_PEER_MTLS_PORT": "443",
                    "LOG_DIR": str(root / "logs"),
                }, clear=False):
                    result = Settings.from_env()
                DroneCertificateManager(result).ensure_certificate()
                return result

            def pair(receiver, sender_settings, sender_id):
                cert = DroneCertificateManager(sender_settings).ensure_certificate()["public_certificate"]
                cert_path, fingerprint = _save_local_peer_certificate(receiver, sender_id, cert)
                local_network.save_paired_peer(receiver, {
                    "drone_id": sender_id, "name": sender_id, "hostname": sender_id,
                    "reachable_url": f"https://127.0.0.1/{sender_id}", "scheme": "https",
                    "api_port": 443, "peer_mtls_port": 443,
                    "certificate_fingerprint": fingerprint, "certificate_path": str(cert_path),
                    "certificate_pem": cert,
                })
                return cert, fingerprint

            receiver = settings("receiver")
            attacker = settings("attacker")
            departing = settings("drone-s")
            # Operator-approved pairings: V trusts M and S (S is the member that
            # later announces its own departure).
            pair(receiver, attacker, "attacker")
            s_cert, s_fingerprint = pair(receiver, departing, "drone-s")
            self.assertIsNotNone(local_network.get_paired_peer(receiver, "drone-s"))

            # Attacker M forges an add for S with S's real pinned certificate,
            # at the largest epoch the receiver's ahead-distance gate allows.
            forged_epoch = swarm_membership._MAX_ADD_EPOCH_AHEAD
            swarm_membership.handle_membership_request(receiver, {"records": [{
                "peer_id": "drone-s", "op": "add", "epoch": forged_epoch,
                "origin": "attacker", "card": {
                    "name": "drone-s", "hostname": "drone-s",
                    "reachable_url": "https://127.0.0.1/drone-s", "scheme": "https",
                    "certificate_pem": s_cert, "certificate_fingerprint": s_fingerprint,
                },
            }]}, "attacker")
            stored = swarm_membership._with_state(
                receiver, lambda state: state["records"]["drone-s"])
            self.assertEqual(stored["op"], "add", "precondition: the forged add must be accepted")
            self.assertEqual(stored["epoch"], forged_epoch)

            # S then announces its own departure from its own mTLS identity with
            # a lower epoch. The issue says a forged add must not keep a record
            # from being superseded by a remove with a lower epoch.
            swarm_membership.handle_membership_request(receiver, {"records": [{
                "peer_id": "drone-s", "op": "remove", "epoch": 5,
                "origin": "drone-s", "card": {},
            }]}, "drone-s")

            self.assertIsNone(
                local_network.get_paired_peer(receiver, "drone-s"),
                "a forged high-epoch add kept drone-s paired after its own lower-epoch departure",
            )


if __name__ == "__main__":
    unittest.main()
