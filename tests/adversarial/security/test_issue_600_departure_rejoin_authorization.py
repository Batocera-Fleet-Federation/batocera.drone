"""A departure tombstone must not be bypassed by third-party gossip.

A member that has departed can only be trusted again after the local operator
explicitly pairs it again.  Its certificate is public to every previous member,
so a different paired member must not be able to replay that certificate in a
newer add record and silently reinstate the departed member.
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
from app.transfer.peer_connectivity import _save_local_peer_certificate


class DepartureRejoinAuthorizationSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required to mint test certificates")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {"HTTP_ONLY": "1", "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1"},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.drones = {name: self._settings(name) for name in ("victim", "member", "attacker")}
        for local in self.drones.values():
            for peer in self.drones.values():
                if local is not peer:
                    self._approve(local, peer)

    def _settings(self, name: str) -> Settings:
        root = Path(self.tmp.name) / name
        for folder in ("roms", "bios", "saves", "movies"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
            "MOVIES_ROOT": str(root / "movies"), "DRONE_DEVICE_ID": name,
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite"),
        }, clear=False):
            settings = Settings.from_env()
        DroneCertificateManager(settings).ensure_certificate()
        return settings

    def _approve(self, settings: Settings, other: Settings) -> None:
        certificate = DroneCertificateManager(other).ensure_certificate()
        pem = str(certificate["public_certificate"])
        path, fingerprint = _save_local_peer_certificate(settings, other.device_id, pem)
        peer = local_network.save_paired_peer(settings, {
            "drone_id": other.device_id, "name": other.device_id,
            "reachable_url": "https://127.0.0.1/", "scheme": "https",
            "certificate_fingerprint": fingerprint, "certificate_path": str(path),
            "certificate_pem": pem, "pairing_source": "local_network",
        })
        swarm_membership.record_approved_member(settings, peer)

    def test_other_member_cannot_reinstate_departed_peer_by_replaying_public_certificate(self) -> None:
        victim = self.drones["victim"]
        member = self.drones["member"]
        certificate = DroneCertificateManager(member).ensure_certificate()
        before = swarm_membership.membership_view(victim)["clock"]
        swarm_membership.handle_membership_request(victim, {"records": [{
            "peer_id": "member", "op": "remove", "epoch": before + 1,
            "origin": "member", "card": {},
        }]}, "member")
        self.assertIsNone(local_network.get_paired_peer(victim, "member"))

        # The attacker learned this public certificate while it was paired and
        # submits a newer add as if it were a normal roster introduction.
        swarm_membership.handle_membership_request(victim, {"records": [{
            "peer_id": "member", "op": "add", "epoch": before + 2,
            "origin": "attacker",
            "card": {
                "name": "member", "reachable_url": "https://127.0.0.1/", "scheme": "https",
                "certificate_pem": str(certificate["public_certificate"]),
                "certificate_fingerprint": str(certificate["fingerprint"]),
            },
        }]}, "attacker")

        self.assertIsNone(
            local_network.get_paired_peer(victim, "member"),
            "only an explicit local re-pair may restore a departed peer",
        )


if __name__ == "__main__":
    unittest.main()
