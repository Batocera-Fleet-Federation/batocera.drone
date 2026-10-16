"""Issue #267 UAT: an unresolved identity must stop every pair-by-address dial.

The identity hold is a local safety state, rather than a diagnosis of the peer.
Therefore it must win before a bootstrap GET (for either supported scheme), map to
503 at the admin boundary, and be rechecked before the TLS chain fallback starts
its second live connection.
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.common import identity_provisioning
from app.transfer import peer_connectivity
from app.web import handlers_network


FINGERPRINT = "ab" * 32
HOLD_MESSAGE = "peer pairing is held pending Drone identity verification"


def _hold() -> None:
    identity_provisioning._publish(
        identity_provisioning.State.RECOVERY_REQUIRED,
        "held",
        "adversarial identity hold",
        False,
    )


class PairByAddressIdentityGateUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity_provisioning.reset_runtime_state_for_tests()
        self.addCleanup(identity_provisioning.reset_runtime_state_for_tests)

    def test_held_peer_info_get_never_constructs_or_sends_a_request_for_any_scheme(self) -> None:
        """The gate is enforced by the real published identity state, not a mocked helper."""
        _hold()
        for address in ("http://peer.example:8080", "https://peer.example:8443"):
            with self.subTest(address=address), \
                    mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock, \
                    mock.patch.object(peer_connectivity.ssl, "_create_unverified_context") as context_mock:
                with self.assertRaisesRegex(peer_connectivity.PeerIdentityHold, HOLD_MESSAGE):
                    peer_connectivity._fetch_peer_info(address)
                urlopen_mock.assert_not_called()
                context_mock.assert_not_called()

    def test_held_pair_by_address_returns_local_503_before_any_peer_info_dial(self) -> None:
        handler = handlers_network.HandlersNetworkMixin()
        handler.settings = mock.MagicMock(use_fake_data=False, device_id="drone-self")
        handler._send_json = mock.Mock()
        _hold()

        for address in ("http://peer.example:8080", "https://peer.example:8443"):
            with self.subTest(address=address), \
                    mock.patch.object(handlers_network._local_network, "is_local_mode", return_value=True), \
                    mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock:
                handler._send_json.reset_mock()
                handler._handle_admin_local_peer_pair_by_address({"address": address, "pairing_code": "secret"})
                self.assertEqual(handler._send_json.call_args.args, (503, {"error": HOLD_MESSAGE}))
                urlopen_mock.assert_not_called()

    def test_hold_landing_after_tls_probe_blocks_chain_fallback_dial(self) -> None:
        """The probe itself may finish, but the fallback must observe a newly-held identity."""
        tls = mock.MagicMock()

        def peer_certificate(*_args, **_kwargs) -> bytes:
            _hold()
            return b"leaf-that-does-not-match"

        tls.__enter__.return_value.getpeercert.side_effect = peer_certificate
        probe = mock.MagicMock()
        probe.wrap_socket.return_value = tls

        with mock.patch.object(peer_connectivity.ssl, "_create_unverified_context", return_value=probe), \
                mock.patch.object(peer_connectivity.socket, "create_connection") as connect_mock, \
                mock.patch.object(peer_connectivity, "_presented_chain_pem") as chain_mock:
            with self.assertRaisesRegex(peer_connectivity.PeerIdentityHold, HOLD_MESSAGE):
                peer_connectivity._pairing_tls_context("https://peer.example:8443", FINGERPRINT)

        connect_mock.assert_called_once_with(("peer.example", 8443), timeout=10)
        chain_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
