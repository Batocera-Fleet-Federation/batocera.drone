"""A TLS-paired peer's stored scheme/route cannot be rewritten to cleartext (issue #246).

Unauthenticated announcements no longer touch a paired record, but two other
writers still copy a peer-supplied ``scheme``/``reachable_url`` into it: swarm
membership gossip (``_apply_add``) and the tailnet-probe refresh. A TLS-paired
peer (``paired_over_tls``) must keep dialable https routes, and a record written
before ``paired_over_tls`` existed (pinned certificate, https scheme) must get
the same protection instead of silently staying downgradable.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity, swarm_membership
from app.transfer.drone_network import _certificate_pem_fingerprint
from app.transfer.drone_tls import DroneCertificateManager


class TlsPinnedSchemeRewriteTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("openssl") is None:
            self.skipTest("openssl is required to mint Drone certificates")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.local = self._settings("local-a")
        local_network.set_mode(self.local, local_network.MODE_LOCAL_NETWORK)
        victim = self._settings("victim")
        certificate = DroneCertificateManager(victim).ensure_certificate()
        self.victim_pem = str(certificate["public_certificate"])
        self.victim_fingerprint = _certificate_pem_fingerprint(self.victim_pem).lower()
        local_network.save_paired_peer(self.local, {"drone_id": "introducer", "reachable_url": "https://192.168.1.30"})

    def _settings(self, device_id: str) -> Settings:
        root = Path(self._tmp.name) / device_id
        for name in ("roms", "bios", "saves", "movies"):
            (root / name).mkdir(parents=True, exist_ok=True)
        env = {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "MOVIES_ROOT": str(root / "movies"),
            "DRONE_DEVICE_ID": device_id,
            "LOG_DIR": str(root / "logs"),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
        DroneCertificateManager(settings).ensure_certificate()
        return settings

    def _gossip_http_card(self) -> None:
        record = {
            "peer_id": "victim",
            "op": "add",
            "epoch": 5,
            "origin": "introducer",
            "card": {
                "certificate_pem": self.victim_pem,
                "certificate_fingerprint": self.victim_fingerprint,
                "scheme": "http",
                "api_port": 8080,
                "peer_mtls_port": 8080,
                "reachable_url": "http://10.0.0.66:8080",
            },
        }
        swarm_membership.handle_membership_request(self.local, {"records": [record]}, "introducer")

    def _assert_only_https_routes(self) -> None:
        stored = local_network.get_paired_peer(self.local, "victim")
        self.assertEqual(stored.get("scheme"), "https", "gossip rewrote the TLS-paired peer's scheme to cleartext")
        candidates = peer_connectivity._peer_address_candidates(stored, settings=self.local, peer_id="victim")
        self.assertEqual([c for c in candidates if c.startswith("http://")], [])
        self.assertTrue(candidates, "TLS-paired peer lost every dialable route to a gossiped http card")

    def test_gossip_cannot_rewrite_flagged_tls_peer_to_http(self) -> None:
        local_network.save_paired_peer(
            self.local,
            {
                "drone_id": "victim",
                "scheme": "https",
                "api_port": 443,
                "reachable_url": "https://192.168.1.20",
                "certificate_fingerprint": self.victim_fingerprint,
                "paired_over_tls": True,
            },
        )
        self._gossip_http_card()
        self._assert_only_https_routes()

    def test_gossip_cannot_downgrade_record_paired_before_the_flag_existed(self) -> None:
        # A pre-upgrade record: pinned certificate, https route, no paired_over_tls key.
        local_network.save_paired_peer(
            self.local,
            {
                "drone_id": "victim",
                "scheme": "https",
                "api_port": 443,
                "reachable_url": "https://192.168.1.20",
                "certificate_fingerprint": self.victim_fingerprint,
            },
        )
        self._gossip_http_card()
        stored = local_network.get_paired_peer(self.local, "victim")
        candidates = peer_connectivity._peer_address_candidates(stored, settings=self.local, peer_id="victim")
        self.assertEqual(
            [c for c in candidates if c.startswith("http://")],
            [],
            "a record paired over https before upgrade is still downgradable to a gossiped cleartext route",
        )


if __name__ == "__main__":
    unittest.main()
