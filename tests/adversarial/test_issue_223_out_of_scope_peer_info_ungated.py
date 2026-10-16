"""Issue #223 out-of-scope finding, adjudicated by issue #267: the peer-info fetch honours the hold.

Issue #223 was scoped to the status code of the held pair-by-address reply, so this suite
originally pinned, as evidence, that ``_fetch_peer_info`` still dialed the operator-typed
address while the identity was held (and reported 502 "could not reach" ahead of the hold).

Issue #267 is that finding. It and CLAUDE.md ("outbound peer TLS ... gated" while held)
require the opposite: no outbound request while held, and the local hold status (503)
rather than a network-failure status. The two assertions below therefore now pin the
required behaviour. This revision is an adjudication of the dispute between the old
evidence-pinning expectations and #267, not a weakening: the same scenarios are
exercised and the request count and status code are asserted on the corrected side.
The broader boundaries live in ``test_issue_267_hold_boundaries_uat.py``.
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


class PeerInfoHonoursIdentityHoldTests(unittest.TestCase):
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

    def test_held_pair_by_address_does_not_send_the_peer_info_request(self) -> None:
        """Required by #267: no outbound GET /v1/api/peer/info while the hold is active."""
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response(PEER_INFO)) as urlopen_mock:
            self.handler._handle_admin_local_peer_pair_by_address(
                {"address": "https://peer.example:8443", "pairing_code": ""}
            )

        urlopen_mock.assert_not_called()
        status, _ = self.handler._send_json.call_args.args
        self.assertEqual(status, 503)

    def test_held_pair_by_address_to_an_unreachable_address_reports_the_hold(self) -> None:
        """Required by #267: nothing is dialed, so the local hold (503) is reported, not a 502."""
        with mock.patch.object(peer_connectivity, "urlopen", side_effect=URLError("refused")) as urlopen_mock:
            self.handler._handle_admin_local_peer_pair_by_address(
                {"address": "https://peer.example:8443", "pairing_code": ""}
            )

        urlopen_mock.assert_not_called()
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertNotIn("could not reach", payload["error"])


if __name__ == "__main__":
    unittest.main()
