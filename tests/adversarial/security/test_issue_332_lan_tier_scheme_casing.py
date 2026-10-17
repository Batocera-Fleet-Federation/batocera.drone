"""Issue #332 security: the LAN tier must not downgrade a TLS-pinned peer via scheme casing.

A paired peer whose stored record carries an upper-case ``scheme`` (``HTTP``) and an
``https`` route is still a TLS-required peer (``peer_requires_tls`` lower-cases both).
``LanDirectTransport`` builds its URL from the raw ``scheme`` field, so it emits
``HTTP://<lan-ip>:<port>``. It then rewrites the peer's routes to that URL, which makes
``peer_requires_tls`` return False for the rewritten record, and the candidate list
then contains a cleartext ``http://`` route for a pinned peer.
"""

import unittest
from urllib.parse import urlparse

from app.transfer import local_network, peer_connectivity
from app.transport.lan import LanDirectTransport


class LanTierSchemeCasingTests(unittest.TestCase):
    def setUp(self):
        self.peer = {
            "drone_id": "victim-332",
            "scheme": "HTTP",
            "reachable_url": "HTTPS://10.0.0.5:8443",
            "certificate_fingerprint": "aa" * 32,
            "public_ip": "203.0.113.7",
            "local_ip": "10.0.0.5",
            "api_port": 8443,
            "peer_mtls_port": 8443,
        }
        self.transport = LanDirectTransport(
            lambda request, context: {},
            local_network=lambda: {"public_ip": "203.0.113.7"},
        )

    def test_precondition_peer_is_tls_required(self):
        self.assertTrue(local_network.peer_requires_tls(self.peer))

    def test_lan_url_never_downgrades_a_tls_required_peer(self):
        url = self.transport.lan_url(self.peer)
        self.assertTrue(
            url is None or urlparse(url).scheme == "https",
            f"LAN tier built a cleartext URL for a TLS-required peer: {url!r}",
        )

    def test_lan_rewritten_route_keeps_tls_requirement_and_offers_no_http(self):
        url = self.transport.lan_url(self.peer)
        if url is None:
            return
        lan_peer = {**self.peer, "public_reachable_url": url, "reachable_url": url}
        self.assertTrue(
            local_network.peer_requires_tls(lan_peer),
            "LAN route rewrite cleared the TLS requirement for a pinned peer",
        )
        candidates = peer_connectivity._peer_address_candidates(lan_peer, peer_id="victim-332")
        self.assertFalse(
            [value for value in candidates if urlparse(value).scheme != "https"],
            f"cleartext candidates offered for a TLS-pinned peer: {candidates!r}",
        )


if __name__ == "__main__":
    unittest.main()
