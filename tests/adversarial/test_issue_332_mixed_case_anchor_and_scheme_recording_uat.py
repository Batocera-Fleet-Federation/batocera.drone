"""Issue #332 adversarial UAT: mixed-case HTTPS evidence in pairing and route storage.

Expectations come from the issue and the repository invariant that a TLS-paired peer
is never dialed or recorded over cleartext. In any letter case:

* a discovered (unpinned) HTTPS route must not count as a certificate anchor;
* an operator-typed HTTPS address must be recorded as an HTTPS peer, and an
  operator-typed HTTP address as an HTTP peer, regardless of how the scheme is spelled;
* a pinned peer whose route is HTTPS must keep HTTPS when a later write offers a
  cleartext route in any spelling;
* any TLS-evidence key, in any spelling, must keep the peer's TLS requirement.

No sockets or TLS mocks are used. Only the trust decision and the stored record are
inspected, using the real persistence layer in a temporary userdata root.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity as pc
from app.web import handlers_network

PIN = "aa" * 32
CONFIRMED_PIN = ":".join(["AA"] * 32)
SPELLINGS = ("HTTPS", "Https", "hTTps", "https")
CLEARTEXT = ("HTTP", "Http", "hTTp", "http")


class MixedCaseAnchorAndSchemeRecordingTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        with mock.patch.dict(os.environ, {"USERDATA_ROOT": str(self.root), "DRONE_DEVICE_ID": "local"}, clear=True):
            self.settings = Settings.from_env()
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = self.settings
        self.handler._send_json = mock.Mock()
        self.handler._pair_peer_or_reply = mock.Mock(return_value={"certificate_path": str(self.root / "missing.crt")})

    def pair_by_address(self, address: str, info: dict) -> dict:
        with mock.patch.object(handlers_network._local_network, "is_local_mode", return_value=True), \
                mock.patch.object(handlers_network, "_fetch_peer_info", return_value=info):
            self.handler._handle_admin_local_peer_pair_by_address({"address": address, "pairing_code": "123456"})
        self.handler._pair_peer_or_reply.assert_called_once()
        return self.handler._pair_peer_or_reply.call_args.args[0]

    def test_discovered_https_route_in_any_case_is_not_a_certificate_anchor(self) -> None:
        for spelling in SPELLINGS:
            with self.subTest(spelling=spelling):
                peer = {"drone_id": "victim", "reachable_url": f"{spelling}://cabinet.local", "certificate_fingerprint": ""}
                self.assertFalse(self.handler._pairing_certificate_anchored(peer, ""))
                self.assertFalse(self.handler._pairing_certificate_anchored(peer, "not-a-fingerprint"))

    def test_https_route_in_any_case_is_anchored_only_by_a_real_fingerprint(self) -> None:
        for spelling in SPELLINGS:
            with self.subTest(spelling=spelling):
                peer = {"drone_id": "victim", "reachable_url": f"{spelling}://cabinet.local", "certificate_fingerprint": ""}
                self.assertTrue(self.handler._pairing_certificate_anchored(peer, CONFIRMED_PIN))

    def test_pinned_mixed_case_https_route_anchors_without_operator_confirmation(self) -> None:
        local_network.save_paired_peer(self.settings, {
            "drone_id": "victim", "certificate_fingerprint": PIN, "reachable_url": "HtTpS://cabinet.local",
        })
        peer = {"drone_id": "victim", "reachable_url": "HtTpS://cabinet.local", "certificate_fingerprint": ""}
        self.assertTrue(self.handler._pairing_certificate_anchored(peer, ""))

    def test_plaintext_route_in_any_case_still_needs_an_anchor(self) -> None:
        # Issue #431: the answering certificate is pinned on first contact, so a
        # cleartext route is anchored only by a confirmed or already-pinned fingerprint.
        for spelling in CLEARTEXT:
            with self.subTest(spelling=spelling):
                peer = {"drone_id": "lan", "reachable_url": f"{spelling}://192.168.1.20:8080"}
                self.assertFalse(self.handler._pairing_certificate_anchored(peer, ""))
                self.assertTrue(self.handler._pairing_certificate_anchored(peer, PIN))

    def test_operator_typed_https_address_is_recorded_as_tls_in_any_case(self) -> None:
        for spelling in SPELLINGS:
            with self.subTest(spelling=spelling):
                info = {"drone_id": "victim", "api_port": 8443, "certificate_fingerprint": PIN}
                peer = self.pair_by_address(f"{spelling}://Cabinet.LOCAL:8443", info)
                self.assertEqual(peer["scheme"], "https")
                self.assertEqual(peer["reachable_url"], "https://cabinet.local:8443")
                self.handler._pair_peer_or_reply.reset_mock()

    def test_operator_typed_http_address_is_recorded_as_plaintext_in_any_case(self) -> None:
        for spelling in CLEARTEXT:
            with self.subTest(spelling=spelling):
                info = {"drone_id": "victim", "api_port": 8080, "certificate_fingerprint": PIN}
                peer = self.pair_by_address(f"{spelling}://192.168.1.20:8080", info)
                self.assertEqual(peer["scheme"], "http")
                self.assertTrue(peer["reachable_url"].startswith("http://"))
                self.handler._pair_peer_or_reply.reset_mock()

    def test_cleartext_offer_cannot_move_mixed_case_pinned_peer_to_http(self) -> None:
        for spelling in SPELLINGS:
            with self.subTest(spelling=spelling):
                local_network.save_paired_peer(self.settings, {
                    "drone_id": "victim", "certificate_fingerprint": PIN, "scheme": spelling,
                    "reachable_url": f"{spelling}://cabinet.local:8443", "paired_over_tls": True,
                })
                for cleartext in CLEARTEXT:
                    local_network.save_paired_peer(self.settings, {
                        "drone_id": "victim", "scheme": cleartext,
                        "reachable_url": f"{cleartext}://attacker.local:8080",
                        "advertised_reachable_url": f"{cleartext}://attacker.local:8080",
                    })
                stored = local_network.get_paired_peer(self.settings, "victim")
                self.assertEqual(stored["scheme"], "https")
                self.assertEqual(stored["reachable_url"], f"https://cabinet.local:8443")
                self.assertTrue(stored["paired_over_tls"])
                self.assertFalse(stored["reachable_url"].lower().startswith("http://"))
                self.assertFalse(stored.get("advertised_reachable_url", "").lower().startswith("http://"))

    def test_tls_requirement_is_detected_from_every_mixed_case_evidence_key(self) -> None:
        for key in ("scheme", "protocol"):
            for spelling in SPELLINGS:
                with self.subTest(key=key, spelling=spelling):
                    self.assertTrue(local_network.peer_requires_tls(
                        {"certificate_fingerprint": PIN, key: spelling, "reachable_url": "http://x.local"}))
        for key in ("reachable_url", "advertised_reachable_url", "public_reachable_url"):
            for spelling in SPELLINGS:
                with self.subTest(key=key, spelling=spelling):
                    self.assertTrue(local_network.peer_requires_tls(
                        {"certificate_fingerprint": PIN, key: f"{spelling}://x.local"}))

    def test_plain_http_fleet_keeps_cleartext_routes_in_any_case(self) -> None:
        for spelling in CLEARTEXT:
            with self.subTest(spelling=spelling):
                peer = {"drone_id": "lan", "scheme": spelling, "reachable_url": f"{spelling}://192.168.1.20:8080"}
                self.assertFalse(local_network.peer_requires_tls(peer))
                self.assertEqual(pc._peer_address_candidates(peer), ["http://192.168.1.20:8080"])


if __name__ == "__main__":
    unittest.main()
