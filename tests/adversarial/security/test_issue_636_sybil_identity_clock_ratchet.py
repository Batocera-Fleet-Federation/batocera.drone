"""A paired member must not stop a Drone's publishing by introducing identities it controls."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, swarm_membership
from app.transfer.drone_network import _certificate_pem_fingerprint
from app.transfer.drone_tls import DroneCertificateManager
from app.transfer.peer_connectivity import _save_local_peer_certificate

_MARGIN = 1_000_000
_ENV = {
    "HTTP_ONLY": "1", "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1",
    "DRONE_COMPAT_HTTPS_PORTS": "", "DRONE_CAST_ENABLED": "0",
    "DRONE_HTTP_REDIRECT_PORT": "0", "USE_FAKE_DATA": "0",
}


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


def _cert(settings):
    return DroneCertificateManager(settings).ensure_certificate()["public_certificate"]


def _pair(receiver, other_id, cert):
    cert_path, fingerprint = _save_local_peer_certificate(receiver, other_id, cert)
    local_network.save_paired_peer(receiver, {
        "drone_id": other_id, "name": other_id, "hostname": other_id,
        "reachable_url": f"https://127.0.0.1/{other_id}", "scheme": "https",
        "api_port": 443, "peer_mtls_port": 443,
        "certificate_fingerprint": fingerprint, "certificate_path": str(cert_path),
        "certificate_pem": cert,
    })


def _add(peer_id, cert, origin, epoch=1):
    return {"peer_id": peer_id, "op": "add", "epoch": epoch, "origin": origin, "card": {
        "name": peer_id, "hostname": peer_id, "reachable_url": f"https://127.0.0.1/{peer_id}",
        "scheme": "https", "certificate_pem": cert,
        "certificate_fingerprint": _certificate_pem_fingerprint(cert).lower(),
    }}


def _clock(settings):
    return swarm_membership.membership_view(settings)["clock"]


class SybilIdentityClockRatchetTests(unittest.TestCase):
    def test_introduced_identities_cannot_stop_a_known_member_publish(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl required")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, _ENV, clear=True):
            victim, attacker, receiver, member = (
                _settings(tmp, name) for name in ("victim", "attacker", "receiver", "member")
            )
            fakes = [_settings(tmp, f"fake{index}") for index in (1, 2)]

            # The victim has the attacker and a member paired; the receiver already knows the member.
            _pair(victim, attacker.device_id, _cert(attacker))
            member_cert = _cert(member)
            _pair(victim, member.device_id, member_cert)
            _pair(receiver, victim.device_id, _cert(victim))
            swarm_membership.handle_membership_request(
                receiver, {"records": [_add(member.device_id, member_cert, "victim")]}, "victim"
            )

            # One paired member introduces identities whose keys it holds. New-peer adds
            # are not bounded by the clock, so both are accepted.
            swarm_membership.handle_membership_request(victim, {"records": [
                _add(fake.device_id, _cert(fake), "attacker") for fake in fakes
            ]}, "attacker")

            # Each introduced identity then removes itself. Every removal sits exactly
            # at the per-record margin, so the clock climbs by one margin per identity.
            for fake in fakes:
                swarm_membership.handle_membership_request(victim, {"records": [{
                    "peer_id": fake.device_id, "op": "remove", "epoch": _clock(victim) + _MARGIN,
                    "origin": fake.device_id,
                }]}, fake.device_id)

            # The victim publishes a re-approval of the member the receiver already knows.
            swarm_membership.record_approved_member(
                victim, local_network.get_paired_peer(victim, member.device_id)
            )
            published = next(
                row for row in swarm_membership.membership_view(victim)["records"]
                if row["peer_id"] == member.device_id
            )
            swarm_membership.handle_membership_request(receiver, {"records": [published]}, "victim")
            received = next(
                row for row in swarm_membership.membership_view(receiver)["records"]
                if row["peer_id"] == member.device_id
            )
            self.assertEqual(
                received["epoch"], published["epoch"],
                "One paired member can stop a Drone from introducing new members or publishing "
                "records to its peers (#636 Impact)",
            )


if __name__ == "__main__":
    unittest.main()
