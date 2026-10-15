"""Out-of-scope evidence from issue #266 testing: the errno-less identity-hold text heuristic.

Not an EACCES problem. ``_pair_peer_or_reply`` still classifies any errno-less ``PermissionError``
whose text equals the identity-hold message as this Drone's own identity hold (503), even while the
identity gate is open. Such an error can only come from something else, so the reply tells the
operator that identity verification is holding pairing when it is not.

Reported as a separate issue. These tests are known defects, so they are marked ``expectedFailure``:
they pass while the defect exists and will report "unexpected success" once it is fixed, which is
the signal to drop the decorator. They must not block the #266 delivery.
"""

import unittest
from unittest import mock

from app.transfer import peer_connectivity
from app.web import handlers_network

PEER = {"drone_id": "peer-a", "reachable_url": "http://10.0.0.5:8080"}


class IdentityHoldTextHeuristicOutOfScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler._send_json = mock.Mock()

    @unittest.expectedFailure
    def test_hold_text_without_errno_is_not_reported_as_hold_while_gate_is_open(self) -> None:
        # Repro: gate open (identity resolved), pairing raises an errno-less PermissionError
        # carrying the hold wording from some other source.
        stray = PermissionError(peer_connectivity.PAIRING_IDENTITY_HOLD_MESSAGE)
        with mock.patch.object(handlers_network, "_identity_swarm_networking_allowed", return_value=True), \
                mock.patch.object(handlers_network, "_local_pair_peer", side_effect=stray):
            self.handler._pair_peer_or_reply({"drone_id": "peer-a", "reachable_url": "http://10.0.0.5:8080"}, "x", "")

        status, payload = self.handler._send_json.call_args.args
        self.assertNotEqual(status, 503, "the gate is open, so this is not an identity hold")


if __name__ == "__main__":
    unittest.main()
