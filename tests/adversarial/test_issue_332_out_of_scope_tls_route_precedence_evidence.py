"""Out-of-scope evidence for #332 (non-blocking): TLS route precedence gaps.

These expectations are not part of the mixed-case HTTPS pinning fix. They are kept
as expectedFailure so the defects are documented and a later fix turns them into an
unexpected success (which fails this suite) and forces the marker to be removed.
Both are fail-closed or latent; neither sends a secret over cleartext today.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity as pc

PIN = "aa" * 32


class TlsRoutePrecedenceEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.dict(os.environ, {"USERDATA_ROOT": str(Path(tmp.name)), "DRONE_DEVICE_ID": "local"}, clear=True):
            self.settings = Settings.from_env()

    def test_single_address_helper_offers_the_https_route_that_candidates_offer(self) -> None:
        # Fixed by #507: _peer_address now falls back to the first https candidate instead of
        # returning None when the pre-baked reachable_url is cleartext.
        peer = {
            "drone_id": "victim", "scheme": "HTTPS", "reachable_url": "HTTP://peer.example:8443",
            "api_port": 8443, "tailnet_ip": "100.64.0.9", "certificate_fingerprint": PIN,
        }
        self.assertTrue(all(value.startswith("https://") for value in pc._peer_address_candidates(peer)))
        address = pc._peer_address(peer)
        self.assertIsNotNone(address)
        self.assertTrue(address.startswith("https://"), address)

    @unittest.expectedFailure
    def test_protocol_alias_counts_as_tls_evidence_when_scheme_says_http(self) -> None:
        # Evidence: peer_requires_tls and _peer_address_candidates read scheme before protocol,
        # so a pinned record with scheme HTTP and protocol https is offered cleartext routes.
        # No current writer produces protocol, so this is latent rather than remotely reachable.
        peer = {
            "drone_id": "victim", "scheme": "HTTP", "protocol": "hTTps", "certificate_fingerprint": PIN,
            "reachable_url": "HTTP://peer.example:8443", "api_port": 8443, "tailnet_ip": "100.64.0.9",
        }
        self.assertTrue(local_network.peer_requires_tls(peer))
        self.assertTrue(all(value.startswith("https://") for value in pc._peer_address_candidates(peer)))


if __name__ == "__main__":
    unittest.main()
