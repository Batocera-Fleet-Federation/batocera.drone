"""Plaintext http:// pairing needs an out-of-band certificate anchor (issue #431).

The operator-typed ``http://`` path sends the pairing code and this Drone's
certificate in the clear, then pins whatever certificate the answer carries. With
no anchor an on-path attacker can answer with its own certificate and become the
pinned peer. These tests drive ``_local_pair_peer`` against a real listener that
plays that attacker, and the handler gate in front of it.
"""

import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity
from app.web import handlers_network

PEER_ID = "peer-a"
FINGERPRINT = "ab" * 32


def _make_certificate(directory: Path, name: str) -> tuple[str, str]:
    """Return ``(pem, sha256 fingerprint)`` of a throwaway self-signed certificate."""
    cert, key = directory / f"{name}.crt", directory / f"{name}.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
         "-subj", f"/CN={name}", "-days", "2"],
        check=True, capture_output=True,
    )
    pem = cert.read_text(encoding="utf-8")
    return pem, peer_connectivity._certificate_pem_fingerprint(pem)


class _AnsweringPeer:
    """Plaintext peer that records pair POSTs and answers ``paired`` with a given certificate."""

    def __init__(self, certificate_pem: str, fingerprint: str) -> None:
        self.bodies: list[bytes] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                outer.bodies.append(self.rfile.read(length))
                body = json.dumps({
                    "status": "paired",
                    "drone_id": PEER_ID,
                    "certificate_pem": certificate_pem,
                    "certificate_fingerprint": fingerprint,
                    "api_port": 8080,
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class PlaintextPairingAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        env = {"USERDATA_ROOT": str(tmp / "userdata"), "DRONE_DEVICE_ID": "local-a"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)
        self.real_pem, self.real_fingerprint = _make_certificate(tmp, "real")
        self.attacker_pem, self.attacker_fingerprint = _make_certificate(tmp, "attacker")
        manager = mock.patch.object(peer_connectivity, "DroneCertificateManager")
        manager.start().return_value.ensure_certificate.return_value = {
            "public_certificate": "-----BEGIN CERTIFICATE-----own", "fingerprint": "cc" * 32,
        }
        self.addCleanup(manager.stop)
        self.pin_file = peer_connectivity._local_peer_cert_cache_path(self.settings, PEER_ID)

    def _answering_peer(self, pem: str, fingerprint: str) -> _AnsweringPeer:
        peer = _AnsweringPeer(pem, fingerprint)
        self.addCleanup(peer.close)
        return peer

    def _pair(self, peer: _AnsweringPeer, **kwargs) -> dict:
        return peer_connectivity._local_pair_peer(
            self.settings, {"drone_id": PEER_ID, "reachable_url": peer.url}, "12345678", **kwargs
        )

    def test_unanchored_http_pairing_sends_nothing_and_pins_nothing(self) -> None:
        peer = self._answering_peer(self.attacker_pem, self.attacker_fingerprint)

        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self._pair(peer)

        self.assertEqual(peer.bodies, [], "the pairing code must not be sent without an anchor")
        self.assertFalse(self.pin_file.exists())
        self.assertIsNone(local_network.get_paired_peer(self.settings, PEER_ID))

    def test_announced_fingerprint_is_not_an_anchor_over_http(self) -> None:
        """The attacker's own fingerprint in the peer record (as an announcement would carry) proves nothing."""
        peer = self._answering_peer(self.attacker_pem, self.attacker_fingerprint)

        with self.assertRaisesRegex(ValueError, "fingerprint"):
            peer_connectivity._local_pair_peer(
                self.settings,
                {"drone_id": PEER_ID, "reachable_url": peer.url, "certificate_fingerprint": self.attacker_fingerprint},
                "12345678",
            )

        self.assertEqual(peer.bodies, [])
        self.assertFalse(self.pin_file.exists())

    def test_substituted_certificate_is_refused_when_the_operator_confirmed_another(self) -> None:
        peer = self._answering_peer(self.attacker_pem, self.attacker_fingerprint)

        with self.assertRaises(peer_connectivity.PeerPairingRejected):
            self._pair(peer, confirmed_fingerprint=self.real_fingerprint)

        self.assertFalse(self.pin_file.exists(), "the attacker certificate must never become a pin")
        self.assertIsNone(local_network.get_paired_peer(self.settings, PEER_ID))

    def test_confirmed_fingerprint_allows_http_pairing_and_pins_the_confirmed_certificate(self) -> None:
        peer = self._answering_peer(self.real_pem, self.real_fingerprint)

        with mock.patch("app.transfer.swarm_membership.propagate_membership"):
            stored = self._pair(peer, confirmed_fingerprint=self.real_fingerprint)

        self.assertEqual(len(peer.bodies), 1)
        self.assertEqual(
            peer_connectivity._fingerprint_key(stored["certificate_fingerprint"]),
            peer_connectivity._fingerprint_key(self.real_fingerprint),
        )
        self.assertEqual(self.pin_file.read_text(encoding="utf-8"), self.real_pem)
        self.assertFalse(stored["paired_over_tls"])

    def test_pinned_record_is_an_anchor_for_http_repairing(self) -> None:
        local_network.save_paired_peer(
            self.settings,
            {"drone_id": PEER_ID, "reachable_url": "http://127.0.0.1:1", "certificate_fingerprint": self.real_fingerprint},
        )
        peer = self._answering_peer(self.attacker_pem, self.attacker_fingerprint)

        with self.assertRaises(peer_connectivity.PeerPairingRejected):
            self._pair(peer)

        self.assertFalse(self.pin_file.exists())

    def test_tailnet_auto_pair_over_http_is_unchanged(self) -> None:
        """Code-free tailnet pairing is authorised by the tailnet source address, not an operator-typed secret."""
        peer = self._answering_peer(self.real_pem, self.real_fingerprint)

        with mock.patch("app.transfer.swarm_membership.propagate_membership"):
            peer_connectivity._local_pair_peer(
                self.settings, {"drone_id": PEER_ID, "reachable_url": peer.url}, "", tailnet_auto_pair=True
            )

        self.assertEqual(len(peer.bodies), 1)


class PlaintextPairingHandlerGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler._send_json = mock.Mock()
        self.peer = {"drone_id": PEER_ID, "reachable_url": "http://192.168.1.20:8080"}

    def _pair(self, pairing_code: str, confirmed: str = "", pinned: Optional[dict] = None):
        with mock.patch.object(handlers_network, "_local_pair_peer", return_value={"drone_id": PEER_ID}) as pair, \
                mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=pinned):
            result = self.handler._pair_peer_or_reply(self.peer, pairing_code, confirmed)
        return pair, result

    def test_http_without_a_fingerprint_is_a_400_even_with_an_empty_code(self) -> None:
        for code in ("12345678", ""):
            with self.subTest(code=code):
                self.handler._send_json.reset_mock()
                pair, result = self._pair(code)
                pair.assert_not_called()
                self.assertIsNone(result)
                status, payload = self.handler._send_json.call_args.args
                self.assertEqual(status, 400)
                self.assertTrue(payload["certificate_fingerprint_required"])

    def test_malformed_fingerprint_is_not_an_anchor_over_http(self) -> None:
        pair, result = self._pair("12345678", "not-a-fingerprint")
        pair.assert_not_called()
        self.assertEqual(self.handler._send_json.call_args.args[0], 400)

    def test_http_with_a_confirmed_or_pinned_fingerprint_is_forwarded(self) -> None:
        pair, _ = self._pair("12345678", FINGERPRINT)
        self.assertEqual(pair.call_args.kwargs["confirmed_fingerprint"], FINGERPRINT)
        pair, _ = self._pair("12345678", pinned={"certificate_fingerprint": FINGERPRINT})
        pair.assert_called_once()


if __name__ == "__main__":
    unittest.main()
