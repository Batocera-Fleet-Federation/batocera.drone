"""UAT for #508: a TLS-pinned peer stays TLS-only when scheme and protocol disagree.

``scheme`` and ``protocol`` are both read as transport evidence. Whichever alias says
https wins, so neither a conflicting alias nor mixed case can offer cleartext routes
or let a writer downgrade the pinned record.
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


def _pinned(**extra: object) -> dict:
    base = {
        "drone_id": "victim",
        "certificate_fingerprint": PIN,
        "reachable_url": "HTTP://peer.example:8443",
        "api_port": 8443,
        "tailnet_ip": "100.64.0.9",
        "local_ip": ["192.168.1.9"],
    }
    base.update(extra)
    return base


class ProtocolAliasTlsUatTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        with mock.patch.dict(os.environ, {"USERDATA_ROOT": str(self.root), "DRONE_DEVICE_ID": "local"}, clear=True):
            self.settings = Settings.from_env()

    def test_scheme_http_with_protocol_https_is_tls_required(self) -> None:
        self.assertEqual(local_network.peer_scheme({"scheme": "HTTP", "protocol": "hTTps"}), "https")
        self.assertTrue(local_network.peer_requires_tls(_pinned(scheme="HTTP", protocol="hTTps")))

    def test_protocol_https_alone_is_tls_required(self) -> None:
        self.assertTrue(local_network.peer_requires_tls(_pinned(protocol="https")))

    def test_scheme_https_with_protocol_http_is_tls_required(self) -> None:
        self.assertEqual(local_network.peer_scheme({"scheme": "https", "protocol": "HTTP"}), "https")
        self.assertTrue(local_network.peer_requires_tls(_pinned(scheme="https", protocol="HTTP")))

    def test_plain_http_aliases_do_not_count_as_tls(self) -> None:
        self.assertEqual(local_network.peer_scheme({"scheme": "HTTP", "protocol": "http"}), "http")
        self.assertFalse(local_network.peer_requires_tls({"drone_id": "plain", "scheme": "HTTP", "protocol": "http"}))

    def test_unset_scheme_is_not_tls_evidence(self) -> None:
        self.assertEqual(local_network.peer_scheme({}), "")
        self.assertFalse(local_network.peer_requires_tls({"drone_id": "plain", "certificate_fingerprint": "", "scheme": ""}))

    def test_route_candidates_never_offer_cleartext_for_protocol_https(self) -> None:
        peer = _pinned(scheme="HTTP", protocol="hTTps")
        candidates = pc._peer_address_candidates(peer)
        self.assertTrue(candidates)
        self.assertTrue(all(value.startswith("https://") for value in candidates), candidates)
        self.assertEqual(pc._peer_address_candidates(peer)[0], "https://100.64.0.9:8443")

    def test_single_address_is_https_for_protocol_https(self) -> None:
        address = pc._peer_address(_pinned(scheme="HTTP", protocol="hTTps"))
        self.assertIsNotNone(address)
        self.assertTrue(address.startswith("https://"), address)

    def test_writer_cannot_downgrade_protocol_https_pinned_record(self) -> None:
        previous = _pinned(scheme="https", protocol="https", paired_over_tls=True)
        local_network.save_paired_peer(self.settings, dict(previous))
        local_network.save_paired_peer(self.settings, {
            "drone_id": "victim",
            "scheme": "HTTP",
            "protocol": "HTTP",
            "reachable_url": "http://peer.example:8443",
            "certificate_fingerprint": PIN,
        })
        stored = local_network._load_peer_map(self.settings, "local_paired_peers")["victim"]
        self.assertTrue(local_network.peer_requires_tls(stored))
        self.assertNotEqual(str(stored.get("scheme", "https")).lower(), "http")
        self.assertNotEqual(str(stored.get("protocol", "https")).lower(), "http")
        self.assertTrue(all(value.startswith("https://") for value in pc._peer_address_candidates(stored)))


if __name__ == "__main__":
    unittest.main()
