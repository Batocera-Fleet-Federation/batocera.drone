"""A peer paired over TLS is never dialed over plaintext http (issue #246)."""

import ssl
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

    def test_https_scheme_casing_cannot_skip_missing_pin_checks(self) -> None:
        for scheme in ("https", "HTTPS", "hTtPs"):
            for method in ("GET", "POST"):
                with self.subTest(scheme=scheme, method=method), \
                        mock.patch.object(peer_connectivity, "urlopen") as opened:
                    url = f"{scheme}://peer.local/peer/inventory"
                    with self.assertRaises(ssl.SSLError):
                        if method == "GET":
                            peer_connectivity._peer_get_json(url, self.settings, peer_id="victim")
                        else:
                            peer_connectivity._peer_post_json(url, {}, self.settings, peer_id="victim")
                    opened.assert_not_called()

    def test_uppercase_https_uses_pinned_context(self) -> None:
        pin = Path(self._tmp.name) / "pin.crt"
        with mock.patch.object(peer_connectivity.ssl, "create_default_context") as create, \
                mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True):
            context = peer_connectivity._drone_client_ssl_context(
                self.settings, "HTTPS://peer.local/peer/inventory", verify=True, cafile=pin,
            )
        self.assertIs(context, create.return_value)
        create.assert_called_once_with(cafile=str(pin))

    def test_persisted_and_legacy_routes_have_canonical_schemes(self) -> None:
        peer = {"drone_id": "victim", "scheme": "HTTPS", "reachable_url": "hTtPs://peer.local",
                "certificate_fingerprint": FINGERPRINT}
        stored = local_network.save_paired_peer(self.settings, peer)
        self.assertEqual(stored["scheme"], "https")
        self.assertEqual(stored["reachable_url"], "https://peer.local")
        self.assertEqual(peer_connectivity._peer_address_candidates(peer), ["https://peer.local"])
        self.assertEqual(peer_connectivity._peer_address(peer), "https://peer.local")

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

    def test_save_paired_peer_ignores_cleartext_rewrite_of_legacy_tls_record(self) -> None:
        # No paired_over_tls key: a record written before the flag existed.
        local_network.save_paired_peer(
            self.settings,
            {"drone_id": "victim", "scheme": "https", "reachable_url": "https://192.168.1.20", "certificate_fingerprint": FINGERPRINT},
        )
        local_network.save_paired_peer(
            self.settings,
            {"drone_id": "victim", "scheme": "http", "reachable_url": "http://10.0.0.66:8080", "advertised_reachable_url": "http://10.0.0.66:8080"},
        )
        stored = local_network.get_paired_peer(self.settings, "victim")
        self.assertEqual(stored["scheme"], "https")
        self.assertEqual(stored["reachable_url"], "https://192.168.1.20")
        self.assertNotIn("advertised_reachable_url", stored)
        self.assertTrue(peer_connectivity._peer_requires_tls(stored))


if __name__ == "__main__":
    unittest.main()


class CodeAuthorizedPairCannotDowngradeTests(unittest.TestCase):
    """A valid pairing code must not clear paired_over_tls via scheme=http (issue #331)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = {"USERDATA_ROOT": str(Path(self._tmp.name) / "userdata"), "DRONE_DEVICE_ID": "local-a"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)
        local_network.save_paired_peer(
            self.settings,
            {"drone_id": "victim", "scheme": "https", "reachable_url": "https://192.168.1.20",
             "certificate_fingerprint": FINGERPRINT, "paired_over_tls": True},
        )

    def test_save_paired_peer_keeps_tls_flag_when_overwritten_with_false(self) -> None:
        stored = local_network.save_paired_peer(
            self.settings,
            {"drone_id": "victim", "scheme": "http", "reachable_url": "http://10.0.0.66",
             "paired_over_tls": False},
        )
        self.assertTrue(stored["paired_over_tls"])
        self.assertEqual(stored["reachable_url"], "https://192.168.1.20")
        self.assertEqual(stored["scheme"], "https")

    def test_pair_request_with_http_scheme_is_refused_for_tls_peer(self) -> None:
        from app.web import handlers_peer

        handler = handlers_peer.HandlersPeerMixin()
        handler.settings = self.settings
        handler.client_address = ("10.0.0.66", 12345)
        handler.server = mock.MagicMock()
        handler._send_json = mock.MagicMock()
        payload = {"pairing_code": "123456", "drone_id": "victim", "scheme": "http",
                   "certificate_pem": "-----BEGIN CERTIFICATE-----fake"}
        with mock.patch.object(handlers_peer._local_network, "validate_pairing_code", return_value=True), \
                mock.patch.object(handlers_peer, "_save_local_peer_certificate") as save_cert:
            handler._handle_peer_pair(payload)
        self.assertEqual(handler._send_json.call_args[0][0], 409)
        save_cert.assert_not_called()
        stored = local_network.get_paired_peer(self.settings, "victim")
        self.assertTrue(stored["paired_over_tls"])
        self.assertEqual(stored["reachable_url"], "https://192.168.1.20")
