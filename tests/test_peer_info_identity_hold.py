"""The peer-info fetch and the pairing TLS probe honour the identity hold (#267)."""

from __future__ import annotations

import unittest
from unittest import mock

from app.transfer import peer_connectivity
from app.web import handlers_network

FINGERPRINT = "ab" * 32


class PeerInfoIdentityHoldTests(unittest.TestCase):
    def test_hold_during_context_setup_blocks_the_next_dial(self) -> None:
        for operation, dial_name in (
            (lambda: peer_connectivity._fetch_peer_info("https://peer.example:8443"), "urlopen"),
            (lambda: peer_connectivity._pairing_tls_context("https://peer.example:8443", FINGERPRINT),
             "socket.create_connection"),
        ):
            with self.subTest(dial=dial_name):
                allowed = True

                def create_context():
                    nonlocal allowed
                    allowed = False
                    return mock.MagicMock()

                with mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed",
                                       side_effect=lambda: allowed), \
                        mock.patch.object(peer_connectivity.ssl, "_create_unverified_context",
                                          side_effect=create_context), \
                        mock.patch(f"{peer_connectivity.__name__}.{dial_name}") as dial:
                    with self.assertRaises(peer_connectivity.PeerIdentityHold):
                        operation()
                dial.assert_not_called()

    def test_fetch_peer_info_does_not_dial_while_held(self) -> None:
        for address in ("https://peer.example:8443", "http://peer.example:8080"):
            with self.subTest(address=address):
                with mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=False), \
                        mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock:
                    with self.assertRaises(peer_connectivity.PeerIdentityHold):
                        peer_connectivity._fetch_peer_info(address)
                urlopen_mock.assert_not_called()

    def test_pair_by_address_replies_503_without_dialing_while_held(self) -> None:
        handler = handlers_network.HandlersNetworkMixin()
        handler.settings = mock.MagicMock(use_fake_data=False)
        handler.settings.device_id = "drone-self"
        handler._send_json = mock.Mock()
        with mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=False), \
                mock.patch.object(handlers_network._local_network, "is_local_mode", return_value=True), \
                mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock:
            handler._handle_admin_local_peer_pair_by_address(
                {"address": "https://peer.example:8443", "pairing_code": "", "certificate_fingerprint": FINGERPRINT}
            )
        urlopen_mock.assert_not_called()
        status, payload = handler._send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertNotIn("reach", payload["error"].lower())

    def test_pairing_tls_probe_does_not_dial_when_hold_lands_first(self) -> None:
        with mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=False), \
                mock.patch.object(peer_connectivity.socket, "create_connection") as connect:
            with self.assertRaises(peer_connectivity.PeerIdentityHold):
                peer_connectivity._pairing_tls_context("https://peer.example:8443", FINGERPRINT)
        connect.assert_not_called()

    def test_pairing_tls_probe_does_not_fetch_chain_when_hold_lands_mid_flight(self) -> None:
        tls = mock.MagicMock()
        tls.__enter__.return_value.getpeercert.return_value = b"not-the-pinned-leaf"
        probe = mock.MagicMock()
        probe.wrap_socket.return_value = tls
        with mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", side_effect=[True, True, False]), \
                mock.patch.object(peer_connectivity.ssl, "_create_unverified_context", return_value=probe), \
                mock.patch.object(peer_connectivity.socket, "create_connection"), \
                mock.patch.object(peer_connectivity, "_presented_chain_pem") as chain:
            with self.assertRaises(peer_connectivity.PeerIdentityHold):
                peer_connectivity._pairing_tls_context("https://peer.example:8443", FINGERPRINT)
        chain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
