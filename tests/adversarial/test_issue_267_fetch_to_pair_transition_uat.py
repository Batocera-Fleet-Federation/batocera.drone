"""Issue #267 UAT: a hold raised between bootstrap and pairing still prevents a POST.

Expected from the identity invariant: pair-by-address consists of a bootstrap GET followed
by a pairing POST.  If local identity enters RECOVERY_REQUIRED after the GET completed,
the second phase is still outbound peer traffic and must be refused locally (503), without
sending the pairing code or certificate to the peer.
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
        identity_provisioning.State.RECOVERY_REQUIRED, "held", "transition test", False
    )


class FetchToPairHoldTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        identity_provisioning.reset_runtime_state_for_tests()
        self.addCleanup(identity_provisioning.reset_runtime_state_for_tests)
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False, device_id="drone-self")
        self.handler._send_json = mock.Mock()

    def test_hold_after_bootstrap_maps_to_503_and_sends_no_pairing_post(self) -> None:
        """The route must preserve the local-hold response from its second phase."""
        peer_info = {
            "drone_id": "peer-a",
            "name": "Peer A",
            "api_port": 8443,
            "peer_mtls_port": 8443,
            "certificate_fingerprint": FINGERPRINT,
        }

        def bootstrap_then_hold(_address: str) -> dict:
            _hold()
            return peer_info

        with mock.patch.object(handlers_network._local_network, "is_local_mode", return_value=True), \
                mock.patch.object(handlers_network, "_fetch_peer_info", side_effect=bootstrap_then_hold), \
                mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock, \
                mock.patch.object(peer_connectivity, "DroneCertificateManager") as certificate_manager:
            self.handler._handle_admin_local_peer_pair_by_address(
                {
                    "address": "https://peer.example:8443",
                    "pairing_code": "operator-secret",
                    "certificate_fingerprint": FINGERPRINT,
                }
            )

        # Entry to _local_pair_peer observes the transition before it reads local
        # certificate material, constructs a request, or sends the operator secret.
        certificate_manager.assert_not_called()
        urlopen_mock.assert_not_called()
        self.assertEqual(self.handler._send_json.call_args.args, (503, {"error": HOLD_MESSAGE}))


if __name__ == "__main__":
    unittest.main()
