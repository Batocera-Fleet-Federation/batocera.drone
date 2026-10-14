"""A peer paired over TLS is never dialed over plaintext http (issue #246)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity

FINGERPRINT = "aa" * 32


class TlsPairedPeerPlaintextRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = {"USERDATA_ROOT": str(Path(self._tmp.name) / "userdata"), "DRONE_DEVICE_ID": "local-a"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)

    def test_spoofed_http_announcement_does_not_downgrade_paired_peer(self) -> None:
        local_network.save_paired_peer(
            self.settings,
            {"drone_id": "victim", "reachable_url": "https://192.168.1.20", "certificate_fingerprint": FINGERPRINT, "paired_over_tls": True},
        )
        local_network.record_discovered_peer(
            self.settings,
            {
                "service": local_network.DISCOVERY_SERVICE,
                "drone_id": "victim",
                "scheme": "http",
                "api_port": 8080,
                "reachable_url": "http://attacker.local:8080",
            },
            "10.0.0.66",
        )
        stored = local_network.get_paired_peer(self.settings, "victim")
        self.assertEqual(stored["reachable_url"], "https://192.168.1.20")
        candidates = peer_connectivity._peer_address_candidates(stored, settings=self.settings, peer_id="victim")
        self.assertTrue(candidates)
        self.assertTrue(all(value.startswith("https://") for value in candidates), candidates)

    def test_http_candidates_dropped_for_pinned_peer(self) -> None:
        peer = {
            "drone_id": "victim",
            "scheme": "http",
            "api_port": 8080,
            "reachable_url": "http://10.0.0.66:8080",
            "tailnet_ip": "100.64.0.9",
            "certificate_fingerprint": FINGERPRINT,
            "paired_over_tls": True,
        }
        self.assertEqual(peer_connectivity._peer_address_candidates(peer, settings=self.settings, peer_id="victim"), [])

    def test_http_candidates_kept_for_unpinned_peer(self) -> None:
        peer = {"drone_id": "plain", "scheme": "http", "api_port": 8080, "reachable_url": "http://10.0.0.5:8080"}
        self.assertEqual(
            peer_connectivity._peer_address_candidates(peer, settings=self.settings, peer_id="plain"),
            ["http://10.0.0.5:8080"],
        )

    def test_http_paired_peer_with_pinned_certificate_keeps_http_routes(self) -> None:
        peer = {
            "drone_id": "plain",
            "scheme": "http",
            "api_port": 8080,
            "reachable_url": "http://10.0.0.5:8080",
            "certificate_fingerprint": FINGERPRINT,
        }
        self.assertEqual(
            peer_connectivity._peer_address_candidates(peer, settings=self.settings, peer_id="plain"),
            ["http://10.0.0.5:8080"],
        )

    def test_single_address_helper_refuses_plaintext_for_tls_paired_peer(self) -> None:
        peer = {"drone_id": "victim", "scheme": "http", "reachable_url": "http://10.0.0.66", "paired_over_tls": True}
        self.assertIsNone(peer_connectivity._peer_address(peer))
        peer["paired_over_tls"] = False
        self.assertEqual(peer_connectivity._peer_address(peer), "http://10.0.0.66")

    def test_pair_refuses_plaintext_address_for_pinned_peer(self) -> None:
        local_network.save_paired_peer(
            self.settings,
            {"drone_id": "victim", "reachable_url": "http://10.0.0.66:8080", "certificate_fingerprint": FINGERPRINT, "paired_over_tls": True},
        )
        # The discovered row handed to the pair action carries only the announced address.
        peer = {"drone_id": "victim", "reachable_url": "http://10.0.0.66:8080", "scheme": "http"}
        with mock.patch.object(peer_connectivity, "urlopen") as opener, \
                mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True), \
                mock.patch.object(peer_connectivity, "DroneCertificateManager") as manager:
            manager.return_value.ensure_certificate.return_value = {"public_certificate": "pem", "fingerprint": "bb" * 32}
            with self.assertRaises(peer_connectivity.PeerPairingRejected):
                peer_connectivity._local_pair_peer(self.settings, peer, "123456")
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
