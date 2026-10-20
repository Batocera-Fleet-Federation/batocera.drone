"""Issue #403: only ``PeerIdentityHold`` maps to a 503 identity hold in ``_pair_peer_or_reply``.

An errno-less ``PermissionError`` that merely carries the identity-hold wording must not be
reported as identity verification holding pairing, whatever the identity gate currently says.
"""

import unittest
from unittest import mock

from app.transfer import peer_connectivity
from app.transfer.peer_connectivity import PeerIdentityHold
from app.web import handlers_network

PEER = {"drone_id": "peer-a", "reachable_url": "http://10.0.0.5:8080"}
HOLD_TEXT = peer_connectivity.PAIRING_IDENTITY_HOLD_MESSAGE


class HoldTextIsNotIdentityHoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler._send_json = mock.Mock()

    def _pair(self, error: BaseException):
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=error):
            self.assertIsNone(self.handler._pair_peer_or_reply(dict(PEER), "x", "ab" * 32))
        return self.handler._send_json.call_args.args

    def test_hold_text_without_errno_is_a_connection_failure(self) -> None:
        for allowed in (True, False):
            with self.subTest(gate_open=allowed), \
                    mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=allowed):
                status, payload = self._pair(PermissionError(HOLD_TEXT))
                self.assertEqual(status, 502)
                self.assertIn("Could not reach that drone", payload["error"])

    def test_real_identity_hold_is_still_503(self) -> None:
        status, payload = self._pair(PeerIdentityHold(HOLD_TEXT))
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], HOLD_TEXT)

    def test_closed_gate_makes_local_pair_peer_raise_the_dedicated_hold(self) -> None:
        settings = mock.MagicMock()
        with mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=False):
            with self.assertRaises(PeerIdentityHold):
                peer_connectivity._local_pair_peer(settings, dict(PEER), "x", confirmed_fingerprint="ab" * 32)
        # Through the handler, with the real _local_pair_peer, the closed gate is a 503.
        with mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=False):
            self.assertIsNone(self.handler._pair_peer_or_reply(dict(PEER), "", "ab" * 32))
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], HOLD_TEXT)


if __name__ == "__main__":
    unittest.main()
