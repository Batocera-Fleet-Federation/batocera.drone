"""Issue #223: local identity holds must not look like unreachable peers."""

from __future__ import annotations

import unittest
from unittest import mock

from app.transfer.peer_connectivity import PeerIdentityHold
from app.web import handlers_network


FINGERPRINT = "ab" * 32


class PairByAddressIdentityHoldBoundaries(unittest.TestCase):
    """Inject local identity and OS permission failures at the pairing boundary."""

    def setUp(self):
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler._send_json = mock.Mock()
        self.peer = {"drone_id": "peer-a", "reachable_url": "https://peer.example"}

    def test_identity_hold_from_local_pairing_is_service_unavailable(self):
        hold = PeerIdentityHold("peer pairing is held pending Drone identity verification")
        with mock.patch("app.web.handlers_network._local_pair_peer", side_effect=hold):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], str(hold))
        self.assertNotIn("Could not reach that drone", payload["error"])

    def test_bare_permission_error_with_hold_text_is_not_matched_as_a_hold(self):
        """#403: only the dedicated PeerIdentityHold type is a hold; text is never matched."""
        lookalike = PermissionError("peer pairing is held pending Drone identity verification")
        with mock.patch("app.web.handlers_network._local_pair_peer", side_effect=lookalike):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("Could not reach that drone", payload["error"])

    def test_errno_permission_error_remains_a_peer_connection_failure(self):
        denied = PermissionError(13, "Permission denied")
        with mock.patch("app.web.handlers_network._local_pair_peer", side_effect=denied):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("Could not reach that drone", payload["error"])

