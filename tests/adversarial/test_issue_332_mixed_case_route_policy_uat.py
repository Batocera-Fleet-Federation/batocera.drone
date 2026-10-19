"""Issue #332 adversarial UAT: mixed-case scheme spellings cannot shed the TLS requirement.

Expectations come from the issue: any evidence that a peer was paired over HTTPS
(in any letter case, in any scheme-bearing field) must make every route the
transport layer offers HTTPS, and a plaintext-only or malformed route must never be
offered. Plain-HTTP fleets with no TLS evidence must keep their HTTP routes, so the
fix must not over-correct. No sockets are used; these are policy-level boundaries.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity as pc
from app.transport.lan import LanDirectTransport

PIN = "aa" * 32


class MixedCaseTlsRoutePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        with mock.patch.dict(os.environ, {"USERDATA_ROOT": str(self.root), "DRONE_DEVICE_ID": "local"}, clear=True):
            self.settings = Settings.from_env()

    def test_every_tls_evidence_spelling_leaves_only_https_candidates(self) -> None:
        evidence = [
            {"scheme": "HTTPS"},
            {"protocol": "hTTps"},
            {"scheme": " Https "},
            {"reachable_url": "HtTpS://peer.example:8443"},
            {"advertised_reachable_url": "HTTPS://peer.example:8443"},
            {"public_reachable_url": "hTTPS://peer.example:8443"},
        ]
        for extra in evidence:
            with self.subTest(evidence=extra):
                peer = {
                    "drone_id": "victim", "scheme": "HTTP", "reachable_url": "HTTP://peer.example:8443",
                    "api_port": 8443, "tailnet_ip": "100.64.0.9", "local_ip": "192.168.1.9",
                    "certificate_fingerprint": PIN, **extra,
                }
                if "protocol" in extra:
                    # protocol-only evidence: no conflicting scheme field is written by any producer.
                    peer["scheme"] = None
                candidates = pc._peer_address_candidates(peer)
                self.assertTrue(candidates)
                self.assertTrue(all(value.startswith("https://") for value in candidates), candidates)

    def test_paired_over_tls_flag_without_fingerprint_still_refuses_cleartext(self) -> None:
        for scheme in ("HTTP", "hTtP"):
            with self.subTest(scheme=scheme):
                peer = {"drone_id": "victim", "paired_over_tls": True, "scheme": scheme,
                        "reachable_url": "HTTP://peer.example:8080", "api_port": 8080}
                self.assertTrue(local_network.peer_requires_tls(peer))
                self.assertEqual(pc._peer_address_candidates(peer), [])
                self.assertIsNone(pc._peer_address(peer))

    def test_plain_http_fleet_keeps_http_routes_in_any_casing(self) -> None:
        # Guards against over-correction: no TLS evidence means HTTP stays HTTP.
        for peer in (
            {"drone_id": "p", "scheme": "HTTP", "reachable_url": "HTTP://peer.example:8080", "api_port": 8080},
            {"drone_id": "p", "scheme": "hTTp", "reachable_url": "hTTp://peer.example:8080",
             "certificate_fingerprint": PIN, "api_port": 8080},
        ):
            with self.subTest(peer=peer["scheme"]):
                self.assertFalse(local_network.peer_requires_tls(peer))
                self.assertEqual(pc._peer_address_candidates(peer), ["http://peer.example:8080"])
                self.assertEqual(pc._peer_address(peer), "http://peer.example:8080")

    def test_malformed_scheme_routes_are_never_offered(self) -> None:
        for raw in ("HTTPS :// peer.example", "https:\t//peer.example", "HTTPS://user:pw@peer.example",
                    "HTTPS://peer.example#frag", "HTTPS://peer.example:0", "HTTPS://", "hxxps://peer.example"):
            with self.subTest(raw=raw):
                peer = {"drone_id": "victim", "scheme": "HTTPS", "reachable_url": raw,
                        "certificate_fingerprint": PIN, "paired_over_tls": True, "api_port": 8443}
                for candidate in pc._peer_address_candidates(peer):
                    self.assertNotIn(raw, candidate)
                    self.assertNotIn("pw", candidate)
                    self.assertNotIn("#frag", candidate)
                    self.assertNotIn(":0", candidate)
                address = pc._peer_address(peer)
                self.assertTrue(address is None or address.startswith("https://"), address)

    def test_lan_tier_never_builds_cleartext_for_any_tls_spelling(self) -> None:
        for scheme in ("HTTP", "hTTp", "http"):
            for flag in ({"paired_over_tls": True}, {"reachable_url": "HTTPS://192.168.1.5:8443"}):
                with self.subTest(scheme=scheme, flag=flag):
                    peer = {"drone_id": "victim", "scheme": scheme, "certificate_fingerprint": PIN,
                            "local_ip": "192.168.1.5", "api_port": 8443, **flag}
                    url = LanDirectTransport._peer_url(peer, "192.168.1.5")
                    self.assertEqual(url, "https://192.168.1.5:8443")

    def test_sticky_tls_requirement_survives_mixed_case_downgrade_write(self) -> None:
        local_network.save_paired_peer(self.settings, {
            "drone_id": "victim", "scheme": "HTTPS", "reachable_url": "HTTPS://peer.example:8443",
            "certificate_fingerprint": PIN, "paired_over_tls": True, "api_port": 8443,
        })
        for downgrade in ({"scheme": "HTTP", "reachable_url": "HTTP://peer.example:8080", "paired_over_tls": False},
                          {"scheme": "hTTp", "reachable_url": "hTTp://peer.example:8080", "paired_over_tls": False},
                          {"protocol": "HTTP", "advertised_reachable_url": "HTTP://peer.example:8080"}):
            with self.subTest(downgrade=downgrade):
                local_network.save_paired_peer(self.settings, {"drone_id": "victim", **downgrade})
                stored = local_network.get_paired_peer(self.settings, "victim")
                self.assertTrue(local_network.peer_requires_tls(stored))
                self.assertTrue(stored.get("paired_over_tls"))
                self.assertEqual(str(stored.get("scheme")).lower(), "https")
                self.assertTrue(str(stored.get("reachable_url")).startswith("https://"))
                self.assertFalse(str(stored.get("advertised_reachable_url") or "").startswith("http://"))

    def test_stored_scheme_is_canonical_lowercase_for_plaintext_peer(self) -> None:
        local_network.save_paired_peer(self.settings, {
            "drone_id": "plain", "scheme": "HtTp", "reachable_url": "HTTP://peer.example:8080", "api_port": 8080,
        })
        stored = local_network.get_paired_peer(self.settings, "plain")
        self.assertEqual(stored.get("scheme"), "http")
        self.assertEqual(stored.get("reachable_url"), "http://peer.example:8080")
        self.assertFalse(local_network.peer_requires_tls(stored))


if __name__ == "__main__":
    unittest.main()
