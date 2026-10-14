"""Peer-side pairing failures surface as 502, never as an unhandled 500 (issue #198)."""

import io
import json
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from app.web import handlers_network


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
            result = self.handler._pair_peer_or_reply(self.peer, "not-a-code")

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
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x"))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("HTTP 500", payload["error"])

    def test_unreachable_peer_becomes_502(self) -> None:
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=URLError("timed out")):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x"))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("Could not reach that drone", payload["error"])

    def test_successful_pairing_is_returned_without_a_response(self) -> None:
        paired = {"drone_id": "peer-a", "certificate_path": ""}
        with mock.patch.object(handlers_network, "_local_pair_peer", return_value=paired):
            self.assertEqual(self.handler._pair_peer_or_reply(self.peer, "CODE"), paired)
        self.handler._send_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
