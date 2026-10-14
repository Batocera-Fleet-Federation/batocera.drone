"""Out-of-scope adversarial finding for issue #223: a non-"paired" peer answer escapes as a 500.

``_local_pair_peer`` raises ``RuntimeError("peer did not accept pairing request")`` when a
peer answers HTTP 200 with a status other than ``paired``. ``_pair_peer_or_reply`` catches
only ``HTTPError``, ``URLError`` and ``OSError``, so the RuntimeError escapes the handler.
The request dispatcher then answers a generic HTTP 500. This is the same unhandled-500
class as #198, which mapped the peer's HTTP rejection. It is not the identity hold, so it
is reported as out of scope.

Adjudicated under issue #224, which requires this case to answer 502 rather than escape as
an unhandled 500. The test originally pinned the escaping RuntimeError as evidence of the
defect; it now asserts the required mapping so it stays a regression guard.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from app.common import identity_provisioning
from app.transfer import peer_connectivity
from app.web import handlers_network


class PeerRejectionEscapesHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        identity_provisioning.reset_runtime_state_for_tests()
        self.addCleanup(identity_provisioning.reset_runtime_state_for_tests)
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler.settings.device_id = "drone-self"
        self.handler._send_json = mock.Mock()
        for target, attribute, kwargs in (
            (handlers_network._local_network, "is_local_mode", {"return_value": True}),
            (handlers_network._local_network, "get_paired_peer", {"return_value": {}}),
            (handlers_network._local_network, "save_paired_peer", {}),
            (peer_connectivity._local_network, "discovery_payload", {"return_value": {}}),
            (peer_connectivity, "DroneCertificateManager", {}),
            (peer_connectivity, "_drone_scheme", {"return_value": "http"}),
            (peer_connectivity, "_drone_advertised_api_port", {"return_value": 8080}),
            (peer_connectivity, "_drone_advertised_peer_mtls_port", {"return_value": 8080}),
        ):
            patcher = mock.patch.object(target, attribute, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_non_paired_peer_answer_is_mapped_to_502_not_raised(self) -> None:
        """Issue #224: a 200 without status=paired is a gateway error reply, not an escaping exception."""
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"status": "queued"}).encode("utf-8")
        peer = {"drone_id": "peer-a", "reachable_url": "http://peer.example:8080"}
        with mock.patch.object(peer_connectivity, "urlopen", return_value=response):
            self.assertIsNone(self.handler._pair_peer_or_reply(peer, "x", ""))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertNotIn("queued", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
