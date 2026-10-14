"""Peer-side pairing failures surface as 502, never as an unhandled 500 (issue #198)."""

import io
import json
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from app.transfer import peer_connectivity
from app.transfer.peer_connectivity import PeerPairingRejected
from app.web import handlers_network

FINGERPRINT = "ab" * 32


def _http_error(code: int, body: dict) -> HTTPError:
    return HTTPError(
        url="https://peer.example/v1/api/peer/pair",
        code=code,
        msg="Forbidden",
        hdrs=None,
        fp=io.BytesIO(json.dumps(body).encode("utf-8")),
    )


class PairPeerErrorMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler._send_json = mock.Mock()
        self.peer = {"drone_id": "peer-a", "reachable_url": "https://peer.example"}

    def test_peer_403_becomes_502_with_peer_message(self) -> None:
        error = _http_error(403, {"error": "invalid or expired pairing code"})
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=error):
            result = self.handler._pair_peer_or_reply(self.peer, "not-a-code", FINGERPRINT)

        self.assertIsNone(result)
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("HTTP 403", payload["error"])
        self.assertIn("invalid or expired pairing code", payload["error"])

    def test_peer_error_without_json_body_still_maps_to_502(self) -> None:
        error = HTTPError(
            url="https://peer.example/v1/api/peer/pair",
            code=500,
            msg="boom",
            hdrs=None,
            fp=io.BytesIO(b"<html>not json</html>"),
        )
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=error):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("HTTP 500", payload["error"])

    def test_unreachable_peer_becomes_502(self) -> None:
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=URLError("timed out")):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("Could not reach that drone", payload["error"])

    def test_identity_hold_becomes_503_not_unreachable(self) -> None:
        hold = PermissionError("peer pairing is held pending Drone identity verification")
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=hold):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], str(hold))
        self.assertNotIn("Could not reach", payload["error"])

    def test_os_level_permission_error_is_still_a_connection_failure(self) -> None:
        denied = PermissionError(13, "Permission denied")
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=denied):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("Could not reach that drone", payload["error"])

    def test_peer_200_without_paired_status_becomes_502(self) -> None:
        rejected = PeerPairingRejected("peer did not accept pairing request")
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=rejected):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"], "That drone did not accept the pairing request.")

    def test_peer_200_with_non_json_body_is_a_rejection_not_a_500(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"<html>proxy page</html>"
        self.handler.settings.device_id = "drone-self"
        patches = [
            mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True),
            mock.patch.object(peer_connectivity._local_network, "is_local_mode", return_value=True),
            mock.patch.object(peer_connectivity._local_network, "get_paired_peer", return_value={}),
            mock.patch.object(peer_connectivity._local_network, "discovery_payload", return_value={}),
            mock.patch.object(peer_connectivity, "DroneCertificateManager"),
            mock.patch.object(peer_connectivity, "_drone_scheme", return_value="http"),
            mock.patch.object(peer_connectivity, "_drone_advertised_api_port", return_value=8080),
            mock.patch.object(peer_connectivity, "_drone_advertised_peer_mtls_port", return_value=8080),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch.object(peer_connectivity, "urlopen", return_value=response):
            self.assertIsNone(
                self.handler._pair_peer_or_reply({"drone_id": "peer-a", "reachable_url": "http://peer.example:8080"}, "x", "")
            )

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertNotIn("proxy page", payload["error"])

    def test_successful_pairing_is_returned_without_a_response(self) -> None:
        paired = {"drone_id": "peer-a", "certificate_path": ""}
        with mock.patch.object(handlers_network, "_local_pair_peer", return_value=paired):
            self.assertEqual(self.handler._pair_peer_or_reply(self.peer, "CODE", FINGERPRINT), paired)
        self.handler._send_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
