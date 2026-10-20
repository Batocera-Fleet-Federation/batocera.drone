"""Peer JSON bodies are read with a byte cap (issue #437).

The operator-typed ``http://`` pair-by-address path is unauthenticated, so a hostile
answer must not make the Drone buffer an unbounded body. These tests drive the real
readers against a loopback listener that plays that attacker.
"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity

PEER_ID = "peer-a"
FINGERPRINT = "ab" * 32


class _BodyPeer:
    """Loopback peer that answers every GET/POST with a fixed body and records how much it wrote."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.bytes_sent = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                try:
                    self.wfile.write(outer.body)
                    outer.bytes_sent = len(outer.body)
                except OSError:
                    pass

            do_GET = _answer  # noqa: N815 - http.server API
            do_POST = _answer  # noqa: N815 - http.server API

            def log_message(self, *_args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _padded_json(payload: dict, size: int) -> bytes:
    """A valid JSON object padded with whitespace to exactly ``size`` bytes."""
    raw = json.dumps(payload).encode("utf-8")
    return raw + b" " * (size - len(raw))


class PeerResponseSizeCapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        env = {"USERDATA_ROOT": str(tmp / "userdata"), "DRONE_DEVICE_ID": "local-a"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)
        manager = mock.patch.object(peer_connectivity, "DroneCertificateManager")
        manager.start().return_value.ensure_certificate.return_value = {
            "public_certificate": "-----BEGIN CERTIFICATE-----own", "fingerprint": "cc" * 32,
        }
        self.addCleanup(manager.stop)
        self.pin_file = peer_connectivity._local_peer_cert_cache_path(self.settings, PEER_ID)

    def _peer(self, body: bytes) -> _BodyPeer:
        peer = _BodyPeer(body)
        self.addCleanup(peer.close)
        return peer

    def test_oversized_pairing_answer_is_a_rejected_pairing_and_pins_nothing(self) -> None:
        limit = peer_connectivity.MAX_PEER_PAIRING_RESPONSE_BYTES
        peer = self._peer(_padded_json({"status": "paired", "drone_id": PEER_ID}, limit + 1))

        with self.assertRaises(peer_connectivity.PeerPairingRejected):
            peer_connectivity._local_pair_peer(
                self.settings, {"drone_id": PEER_ID, "reachable_url": peer.url}, "12345678",
                confirmed_fingerprint=FINGERPRINT,
            )

        self.assertFalse(self.pin_file.exists())
        self.assertIsNone(local_network.get_paired_peer(self.settings, PEER_ID))

    def test_huge_pairing_answer_is_not_buffered(self) -> None:
        sizes: list = []
        real = peer_connectivity._read_capped

        def spy(response, limit):
            original = response.read
            response.read = lambda amount=None: (sizes.append(amount), original(amount))[1]
            return real(response, limit)

        peer = self._peer(b" " * (4 * 1024 * 1024))
        with mock.patch.object(peer_connectivity, "_read_capped", side_effect=spy):
            with self.assertRaises(peer_connectivity.PeerPairingRejected):
                peer_connectivity._local_pair_peer(
                    self.settings, {"drone_id": PEER_ID, "reachable_url": peer.url}, "12345678",
                    confirmed_fingerprint=FINGERPRINT,
                )

        self.assertEqual(sizes, [peer_connectivity.MAX_PEER_PAIRING_RESPONSE_BYTES + 1])

    def test_oversized_peer_info_is_refused(self) -> None:
        limit = peer_connectivity.MAX_PEER_PAIRING_RESPONSE_BYTES
        service = peer_connectivity._local_network.DISCOVERY_SERVICE
        ok = self._peer(_padded_json({"service": service}, limit))
        self.assertEqual(peer_connectivity._fetch_peer_info(ok.url)["service"], service)

        too_big = self._peer(_padded_json({"service": service}, limit + 1))
        with self.assertRaises(peer_connectivity.PeerResponseTooLarge):
            peer_connectivity._fetch_peer_info(too_big.url)

    def test_oversized_peer_get_and_post_json_are_refused(self) -> None:
        limit = peer_connectivity.MAX_PEER_JSON_RESPONSE_BYTES
        ok = self._peer(_padded_json({"ok": True}, limit))
        self.assertEqual(peer_connectivity._peer_get_json(ok.url, self.settings), {"ok": True})
        self.assertEqual(peer_connectivity._peer_post_json(ok.url, {}, self.settings), {"ok": True})

        too_big = self._peer(_padded_json({"ok": True}, limit + 1))
        with self.assertRaises(peer_connectivity.PeerResponseTooLarge):
            peer_connectivity._peer_get_json(too_big.url, self.settings)
        with self.assertRaises(peer_connectivity.PeerResponseTooLarge):
            peer_connectivity._peer_post_json(too_big.url, {}, self.settings)


if __name__ == "__main__":
    unittest.main()
