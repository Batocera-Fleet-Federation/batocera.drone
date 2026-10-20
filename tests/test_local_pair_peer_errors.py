"""Peer-side pairing failures surface as 502, never as an unhandled 500 (issue #198)."""

import io
import json
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from app.common.settings import Settings
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
        hold = handlers_network.PeerIdentityHold("peer pairing is held pending Drone identity verification")
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

    def test_dedicated_identity_hold_type_becomes_503(self) -> None:
        hold = handlers_network.PeerIdentityHold("some other hold wording")
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=hold):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertNotIn("Could not reach", payload["error"])

    def test_local_certificate_access_failure_is_not_reported_as_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"USERDATA_ROOT": tmp, "DRONE_STATE_DATABASE_FILE": f"{tmp}/state.db", "DRONE_DEVICE_ID": "drone-self"}
            with mock.patch.dict("os.environ", env, clear=True):
                settings = Settings.from_env()
            denied = PermissionError(13, "Permission denied", f"{tmp}/drone.key")
            with mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True), \
                mock.patch.object(peer_connectivity._local_network, "is_local_mode", return_value=True), \
                mock.patch.object(peer_connectivity.DroneCertificateManager, "ensure_certificate", side_effect=denied):
                self.handler.settings = settings
                self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 500)
        self.assertNotIn("Could not reach", payload["error"])
        self.assertIn("Permission denied", payload["error"])
        self.assertNotIn("drone.key", payload["error"])

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


class PairingMalformedPortTests(unittest.TestCase):
    """A non-numeric port in a paired answer is a peer rejection, and writes no trust pin (issue #425)."""

    def _run_pairing(self, answer: dict):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(answer).encode("utf-8")
        patches = [
            mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True),
            mock.patch.object(peer_connectivity._local_network, "is_local_mode", return_value=True),
            mock.patch.object(peer_connectivity._local_network, "get_paired_peer", return_value={}),
            mock.patch.object(peer_connectivity._local_network, "discovery_payload", return_value={}),
            mock.patch.object(peer_connectivity, "DroneCertificateManager"),
            mock.patch.object(peer_connectivity, "_drone_scheme", return_value="http"),
            mock.patch.object(peer_connectivity, "_drone_advertised_api_port", return_value=8080),
            mock.patch.object(peer_connectivity, "_drone_advertised_peer_mtls_port", return_value=8080),
            mock.patch.object(peer_connectivity, "urlopen", return_value=response),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        save_pin = mock.patch.object(peer_connectivity, "_save_local_peer_certificate")
        pin = save_pin.start()
        self.addCleanup(save_pin.stop)
        peer = {"drone_id": "peer-a", "reachable_url": "http://peer.example:8080"}
        with mock.patch.object(peer_connectivity._local_network, "save_paired_peer") as save_paired:
            with self.assertRaises(PeerPairingRejected):
                peer_connectivity._local_pair_peer(mock.MagicMock(device_id="drone-self"), peer, "CODE")
        return pin, save_paired

    def test_non_numeric_api_port_is_rejected_before_the_pin_is_written(self) -> None:
        answer = {"status": "paired", "drone_id": "peer-a", "api_port": "not-a-port", "certificate_pem": "x"}
        pin, save_paired = self._run_pairing(answer)

        pin.assert_not_called()
        save_paired.assert_not_called()

    def test_out_of_range_peer_mtls_port_is_rejected_before_the_pin_is_written(self) -> None:
        answer = {"status": "paired", "drone_id": "peer-a", "peer_mtls_port": 70000, "certificate_pem": "x"}
        pin, save_paired = self._run_pairing(answer)

        pin.assert_not_called()
        save_paired.assert_not_called()



if __name__ == "__main__":
    unittest.main()
