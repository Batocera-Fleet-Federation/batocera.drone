"""A paired member must not ratchet the receiver clock through repeated bounded adds."""

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


class IncrementalEpochRatchetTests(unittest.TestCase):
    def test_repeated_allowed_adds_cannot_silence_future_gossip(self):
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

            receiver, attacker = settings("receiver"), settings("attacker")
            cert = DroneCertificateManager(attacker).ensure_certificate()["public_certificate"]
            cert_path, fingerprint = _save_local_peer_certificate(receiver, "attacker", cert)
            local_network.save_paired_peer(receiver, {
                "drone_id": "attacker", "name": "attacker", "hostname": "attacker",
                "reachable_url": "https://127.0.0.1/attacker", "scheme": "https",
                "api_port": 443, "peer_mtls_port": 443,
                "certificate_fingerprint": fingerprint, "certificate_path": str(cert_path),
                "certificate_pem": cert,
            })
            margin = swarm_membership._MAX_ADD_EPOCH_AHEAD
            ceiling = swarm_membership._MAX_EPOCH
            epoch = 0
            while epoch < ceiling:
                epoch = min(ceiling, epoch + margin)
                swarm_membership.handle_membership_request(receiver, {"records": [{
                    "peer_id": "attacker", "op": "add", "epoch": epoch,
                    "origin": "attacker", "card": {
                        "name": "attacker", "hostname": "attacker",
                        "reachable_url": "https://127.0.0.1/attacker", "scheme": "https",
                        "certificate_pem": cert, "certificate_fingerprint": fingerprint,
                    },
                }]}, "attacker")

            # Rejected remote adds must not advance the receiver's local mint
            # clock, even when an attacker repeats records up to the parse
            # ceiling across requests.
            self.assertEqual(
                swarm_membership.membership_view(receiver)["clock"], 0,
                "gossiped adds ratcheted the receiver's local clock",
            )
            minted = swarm_membership._with_state(receiver, lambda state: swarm_membership._mint(state))
            self.assertEqual(minted, 1)
            self.assertLessEqual(minted, margin)


if __name__ == "__main__":
    unittest.main()
