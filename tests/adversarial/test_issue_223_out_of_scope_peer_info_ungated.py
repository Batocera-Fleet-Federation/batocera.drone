"""Out-of-scope adversarial finding for issue #223: the peer-info fetch ignores the identity hold.

Issue #223 is scoped to the status code the held pair-by-address reply uses. It does not
change where the hold is checked. Evidence:

* ``_fetch_peer_info`` (``app/transfer/peer_connectivity.py``) opens an outbound request to
  the operator-typed address with no call to ``swarm_networking_allowed``. Over https it
  uses an unverified TLS context directly, not ``_drone_client_ssl_context`` (the gate that
  refuses while held).
* CLAUDE.md states that a held Drone gates "outbound peer TLS" and that nothing peer-facing
  starts before identity resolves. This request is outbound peer TLS made while held.
* The pairing code is not sent by this request, so the leak is the connection itself, not a
  secret. For an unreachable address the held Drone also reports a network failure (502
  "could not reach") ahead of the hold.

The assertions below pin the current behaviour as evidence. They are kept in their own
suite so the worker can file this finding separately. The hold-refusal assertions for the
pairing POST itself live in ``test_issue_223_real_hold_gate_uat.py`` and are blocking.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock
from urllib.error import URLError

from app.common import identity_provisioning
from app.transfer import peer_connectivity
from app.web import handlers_network

PEER_INFO = {
    "service": peer_connectivity._local_network.DISCOVERY_SERVICE,
    "drone_id": "peer-a",
    "name": "peer-a",
    "api_port": 8443,
    "certificate_fingerprint": "ab" * 32,
}


def _response(payload: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return response


class PeerInfoIgnoresIdentityHoldTests(unittest.TestCase):
    def setUp(self) -> None:
        identity_provisioning.reset_runtime_state_for_tests()
        self.addCleanup(identity_provisioning.reset_runtime_state_for_tests)
        identity_provisioning._publish(
            identity_provisioning.State.RECOVERY_REQUIRED, "held", "adversarial hold", False
        )
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler.settings.device_id = "drone-self"
        self.handler._send_json = mock.Mock()
        for target, attribute, kwargs in (
            (handlers_network._local_network, "is_local_mode", {"return_value": True}),
            (handlers_network._local_network, "save_paired_peer", {}),
        ):
            patcher = mock.patch.object(target, attribute, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_held_pair_by_address_still_sends_the_peer_info_request(self) -> None:
        """Observed: the outbound GET /v1/api/peer/info is made while the hold is active."""
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response(PEER_INFO)) as urlopen_mock:
            self.handler._handle_admin_local_peer_pair_by_address(
                {"address": "https://peer.example:8443", "pairing_code": ""}
            )

        requested = [call.args[0].full_url for call in urlopen_mock.call_args_list]
        self.assertEqual(requested, ["https://peer.example:8443/v1/api/peer/info"])
        status, _ = self.handler._send_json.call_args.args
        self.assertEqual(status, 503)

    def test_held_pair_by_address_to_an_unreachable_address_reports_unreachable(self) -> None:
        """Observed: with the hold active, the network failure is reported ahead of the hold."""
        with mock.patch.object(peer_connectivity, "urlopen", side_effect=URLError("refused")):
            self.handler._handle_admin_local_peer_pair_by_address(
                {"address": "https://peer.example:8443", "pairing_code": ""}
            )

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("could not reach a Drone at", payload["error"])


if __name__ == "__main__":
    unittest.main()
