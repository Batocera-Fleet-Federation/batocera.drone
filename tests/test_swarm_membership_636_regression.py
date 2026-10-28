"""Focused regression for #636: add epochs are bounded and do not ratchet clock."""
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


def _settings(tmp, device_id):
    root = Path(tmp) / device_id
    for name in ("roms", "bios", "saves", "movies"):
        (root / name).mkdir(parents=True, exist_ok=True)
    with mock.patch.dict(os.environ, {
        "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
        "MOVIES_ROOT": str(root / "movies"), "DRONE_DEVICE_ID": device_id,
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite"),
        "HTTPS_PORT": "443", "DRONE_ADVERTISED_API_PORT": "443",
        "DRONE_PEER_MTLS_PORT": "443", "DRONE_ADVERTISED_PEER_MTLS_PORT": "443",
        "LOG_DIR": str(root / "logs"),
    }, clear=False):
        result = Settings.from_env()
    DroneCertificateManager(result).ensure_certificate()
    return result


class AddEpochBoundRegressionTests(unittest.TestCase):
    def test_single_large_add_for_existing_peer_is_rejected(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl required")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
            "HTTP_ONLY": "1", "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1",
            "DRONE_COMPAT_HTTPS_PORTS": "", "DRONE_CAST_ENABLED": "0",
            "DRONE_HTTP_REDIRECT_PORT": "0", "USE_FAKE_DATA": "0",
        }, clear=True):
            victim = _settings(tmp, "victim")
            attacker = _settings(tmp, "attacker")
            cert = DroneCertificateManager(attacker).ensure_certificate()["public_certificate"]
            cert_path, fingerprint = _save_local_peer_certificate(victim, "attacker", cert)
            local_network.save_paired_peer(victim, {
                "drone_id": "attacker", "name": "attacker", "hostname": "attacker",
                "reachable_url": "https://127.0.0.1/attacker", "scheme": "https",
                "api_port": 443, "peer_mtls_port": 443,
                "certificate_fingerprint": fingerprint, "certificate_path": str(cert_path),
                "certificate_pem": cert,
            })
            # Establish a legitimate record for attacker so the next add is for an existing peer.
            swarm_membership.handle_membership_request(victim, {"records": [{
                "peer_id": "attacker", "op": "add", "epoch": 1,
                "origin": "attacker", "card": {
                    "name": "attacker", "hostname": "attacker",
                    "reachable_url": "https://127.0.0.1/attacker", "scheme": "https",
                    "certificate_pem": cert, "certificate_fingerprint": fingerprint,
                },
            }]}, "attacker")
            # Now a forged large epoch for existing peer must be rejected and not ratchet.
            swarm_membership.handle_membership_request(victim, {"records": [{
                "peer_id": "attacker", "op": "add", "epoch": swarm_membership._MAX_EPOCH,
                "origin": "attacker", "card": {
                    "name": "attacker", "hostname": "attacker",
                    "reachable_url": "https://127.0.0.1/attacker", "scheme": "https",
                    "certificate_pem": cert, "certificate_fingerprint": fingerprint,
                },
            }]}, "attacker")
            self.assertEqual(swarm_membership.membership_view(victim)["clock"], 0,
                             "existing-peer large add must not ratchet clock")
            # Minted record must still be propagatable (epoch small).
            minted = swarm_membership._with_state(victim, lambda s: swarm_membership._mint(s))
            self.assertLessEqual(minted, swarm_membership._MAX_ADD_EPOCH_AHEAD)

    def test_minted_epoch_clamped_to_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
            "HTTP_ONLY": "1", "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1",
            "DRONE_COMPAT_HTTPS_PORTS": "", "DRONE_CAST_ENABLED": "0",
            "DRONE_HTTP_REDIRECT_PORT": "0", "USE_FAKE_DATA": "0",
        }, clear=True):
            s = _settings(tmp, "drone")
            swarm_membership._with_state(s, lambda state: state.update(clock=swarm_membership._MAX_EPOCH))
            # Next mint must not exceed ceiling.
            epoch = swarm_membership._with_state(s, lambda state: swarm_membership._mint(state))
            self.assertLessEqual(epoch, swarm_membership._MAX_EPOCH)
            self.assertEqual(epoch, swarm_membership._MAX_EPOCH)


class RemovalClockAndReapprovalTests(unittest.TestCase):
    _ENV = {
        "HTTP_ONLY": "1", "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1",
        "DRONE_COMPAT_HTTPS_PORTS": "", "DRONE_CAST_ENABLED": "0",
        "DRONE_HTTP_REDIRECT_PORT": "0", "USE_FAKE_DATA": "0",
    }

    def test_remote_removal_keeps_clock_and_local_reapproval_outranks_it(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl required")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, self._ENV, clear=True):
            victim = _settings(tmp, "victim")
            member = _settings(tmp, "member")
            cert = DroneCertificateManager(member).ensure_certificate()["public_certificate"]
            cert_path, fingerprint = _save_local_peer_certificate(victim, "member", cert)
            peer = {
                "drone_id": "member", "name": "member", "hostname": "member",
                "reachable_url": "https://127.0.0.1/member", "scheme": "https",
                "api_port": 443, "peer_mtls_port": 443,
                "certificate_fingerprint": fingerprint, "certificate_path": str(cert_path),
                "certificate_pem": cert,
            }
            local_network.save_paired_peer(victim, peer)
            margin = swarm_membership._MAX_REMOVAL_EPOCH_AHEAD
            swarm_membership.handle_membership_request(victim, {"records": [{
                "peer_id": "member", "op": "remove", "epoch": margin, "origin": "member",
            }]}, "member")
            self.assertEqual(swarm_membership.membership_view(victim)["clock"], 0,
                             "a remote removal must not advance the mint clock")
            local_network.save_paired_peer(victim, peer)
            swarm_membership.record_approved_member(victim, peer)
            view = swarm_membership.membership_view(victim)
            record = next(row for row in view["records"] if row["peer_id"] == "member")
            self.assertEqual(record["op"], "add")
            self.assertGreater(record["epoch"], margin)
            self.assertLess(view["clock"], 10)
