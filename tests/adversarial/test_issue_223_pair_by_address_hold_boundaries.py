"""Issue #223 adversarial boundaries: a local identity hold must never read as "unreachable".

Expected, derived from the issue before reading the patch:

* A pair-by-address attempt made while this Drone's identity is unresolved is
  refused by the local hold, not by the peer. The reply must not be the
  "Could not reach that drone" 502 that the issue reports as the bug.
* The hold is local state, so it must stop the attempt before the pairing code
  is written and before any paired-peer row is saved. It must also hold when the
  hold flips between the handler's gates, not only when it is already set.
* The refusal status matches the swarm-networking hold reply the rest of the
  Drone already uses for peer surfaces (503 in ``web/api_routes.py``).
* Every status the pair-by-address operation can return is documented in the
  OpenAPI contract, as the other admin operations do.
"""

import unittest
from unittest import mock

from app.drone_api import OPENAPI_SPEC
from app.transfer import peer_connectivity
from app.web import handlers_network

FINGERPRINT = "ab" * 32
HOLD_MESSAGE = "peer pairing is held pending Drone identity verification"


def _settings() -> mock.MagicMock:
    settings = mock.MagicMock(use_fake_data=False)
    settings.device_id = "drone-self"
    return settings


class PairByAddressHeldIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = _settings()
        self.handler._send_json = mock.Mock()

    def _run_pair_by_address(self, *, held_flags):
        """Drive the real admin handler; only the network and the hold are controlled."""
        fetched = {
            "service": "batocera-drone",
            "drone_id": "peer-a",
            "name": "peer-a",
            "api_port": 8443,
            "certificate_fingerprint": FINGERPRINT,
        }
        with mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", side_effect=held_flags) as gate, \
                mock.patch.object(handlers_network, "_fetch_peer_info", return_value=fetched), \
                mock.patch.object(handlers_network._local_network, "is_local_mode", return_value=True), \
                mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value={}), \
                mock.patch.object(handlers_network._local_network, "save_paired_peer") as save, \
                mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock:
            self.handler._handle_admin_local_peer_pair_by_address(
                {
                    "address": "https://peer.example:8443",
                    "pairing_code": "not-a-code",
                    "certificate_fingerprint": FINGERPRINT,
                }
            )
        return gate, save, urlopen_mock

    def test_held_identity_pair_by_address_is_503_not_unreachable(self) -> None:
        gate, save, urlopen_mock = self._run_pair_by_address(held_flags=[False])

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertEqual(payload, {"error": HOLD_MESSAGE})
        self.assertNotIn("reach", payload["error"].lower())
        urlopen_mock.assert_not_called()
        save.assert_not_called()

    def test_hold_flipping_between_gates_sends_no_pairing_request(self) -> None:
        """The first gate passes, the hold lands before the POST: nothing is sent."""
        peer = {"drone_id": "peer-a", "reachable_url": "http://peer.example:8080"}
        certificate = {"public_certificate": "PEM", "fingerprint": FINGERPRINT}
        with mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", side_effect=[True, False]) as gate, \
                mock.patch.object(peer_connectivity, "DroneCertificateManager") as manager, \
                mock.patch.object(peer_connectivity._local_network, "is_local_mode", return_value=True), \
                mock.patch.object(peer_connectivity._local_network, "get_paired_peer", return_value={}), \
                mock.patch.object(peer_connectivity._local_network, "discovery_payload", return_value={}), \
                mock.patch.object(peer_connectivity, "_drone_scheme", return_value="http"), \
                mock.patch.object(peer_connectivity, "_drone_advertised_api_port", return_value=8080), \
                mock.patch.object(peer_connectivity, "_drone_advertised_peer_mtls_port", return_value=8080), \
                mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock:
            manager.return_value.ensure_certificate.return_value = certificate
            result = self.handler._pair_peer_or_reply(peer, "not-a-code", FINGERPRINT)

        self.assertIsNone(result)
        self.assertEqual(gate.call_count, 2)
        urlopen_mock.assert_not_called()
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertEqual(payload, {"error": HOLD_MESSAGE})

    def test_held_identity_reply_is_the_same_for_the_discovered_peer_route(self) -> None:
        """Both pair routes share ``_pair_peer_or_reply``; the hold must read the same on each."""
        peer = {"drone_id": "peer-a", "reachable_url": "https://peer.example:8443"}
        with mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=False), \
                mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock:
            self.assertIsNone(self.handler._pair_peer_or_reply(peer, "", FINGERPRINT))

        urlopen_mock.assert_not_called()
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertEqual(payload, {"error": HOLD_MESSAGE})


class PairByAddressOpenApiContractTests(unittest.TestCase):
    def test_pair_by_address_documents_the_hold_status(self) -> None:
        operation = OPENAPI_SPEC["paths"]["/admin/local-network/pair-by-address"]["post"]

        self.assertIn("503", operation["responses"])
        self.assertIn("502", operation["responses"])


if __name__ == "__main__":
    unittest.main()
