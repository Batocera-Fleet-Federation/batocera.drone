"""TLS-required peers fall back to an https route when the pre-baked URL is cleartext (issue #507)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity

FINGERPRINT = "aa" * 32


class TlsRouteFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = {"USERDATA_ROOT": str(Path(self._tmp.name) / "userdata"), "DRONE_DEVICE_ID": "local-a"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)

    def test_cleartext_prebaked_url_falls_back_to_https_tailnet_route(self) -> None:
        peer = {
            "drone_id": "victim", "scheme": "HTTPS", "reachable_url": "HTTP://peer.example:8443",
            "api_port": 8443, "tailnet_ip": "100.64.0.9", "certificate_fingerprint": FINGERPRINT,
        }
        self.assertEqual(peer_connectivity._peer_address(peer), "https://100.64.0.9:8443")
        self.assertEqual(peer_connectivity._preferred_peer_address(peer), "https://100.64.0.9:8443")

    def test_cleartext_only_record_still_returns_none(self) -> None:
        peer = {
            "drone_id": "victim", "scheme": "HTTPS", "reachable_url": "http://peer.example:8443",
            "api_port": 8443, "certificate_fingerprint": FINGERPRINT, "paired_over_tls": True,
        }
        self.assertEqual(peer_connectivity._peer_address_candidates(peer), [])
        self.assertIsNone(peer_connectivity._peer_address(peer))

    def test_https_prebaked_url_is_still_preferred(self) -> None:
        peer = {
            "drone_id": "victim", "scheme": "HTTPS", "reachable_url": "https://peer.local:8443",
            "api_port": 8443, "tailnet_ip": "100.64.0.9", "certificate_fingerprint": FINGERPRINT,
        }
        self.assertEqual(peer_connectivity._peer_address(peer), "https://peer.local:8443")


if __name__ == "__main__":
    unittest.main()
