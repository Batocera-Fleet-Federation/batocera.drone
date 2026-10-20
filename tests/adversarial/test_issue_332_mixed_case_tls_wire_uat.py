"""Issue #332 adversarial UAT over real TLS sockets: mixed-case HTTPS keeps the peer pin.

The expectation comes from the issue, not from the implementation: a route whose
scheme is HTTPS in any letter case must be dialed through the pinned peer TLS
context. A certificate other than the pinned one must fail the handshake before any
request reaches the peer, and a plaintext route for the same TLS-paired peer must
never be attempted. No ``urlopen`` or TLS mocks are used; the peers are real local
TLS and HTTP servers, so a downgrade or a missing pin shows up as a served request.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity as pc

SCHEMES = ("HTTPS", "Https", "hTTps", "https")
HEALTH_PATH = "/v1/api/peer/health"


def _fingerprint(pem_text: str) -> str:
    return pc._certificate_pem_fingerprint(pem_text)


class _Peer:
    """A real HTTP(S) peer that counts every request it actually serves."""

    def __init__(self, payload: dict, tls: "tuple[Path, Path] | None" = None) -> None:
        self.requests = 0
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                peer.requests += 1
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(tls[0]), str(tls[1]))
            # A failed client handshake must not stop the accept loop.
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = int(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class MixedCaseTlsWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._keys = tempfile.TemporaryDirectory()
        root = Path(cls._keys.name)
        cls.victim = cls._make_identity(root, "victim")
        cls.attacker = cls._make_identity(root, "attacker")
        cls.victim_pin = _fingerprint(cls.victim[0].read_text(encoding="utf-8"))
        cls.attacker_pin = _fingerprint(cls.attacker[0].read_text(encoding="utf-8"))
        cls.victim_pem = cls.victim[0].read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._keys.cleanup()

    @staticmethod
    def _make_identity(root: Path, name: str) -> "tuple[Path, Path]":
        cert, key = root / f"{name}.crt", root / f"{name}.key"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
             "-out", str(cert), "-days", "1", "-subj", f"/CN={name}",
             "-addext", "subjectAltName=IP:127.0.0.1"],
            check=True, capture_output=True,
        )
        return cert, key

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        with mock.patch.dict(os.environ, {"USERDATA_ROOT": str(self.root), "DRONE_DEVICE_ID": "local"}, clear=True):
            self.settings = Settings.from_env()
        swarm = mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=True)
        swarm.start()
        self.addCleanup(swarm.stop)
        self.health = {"service": local_network.DISCOVERY_SERVICE, "drone_id": "victim", "ok": True}

    def pin_victim(self) -> None:
        pc._save_local_peer_certificate(self.settings, "victim", self.victim_pem)

    def serve(self, identity, payload=None) -> _Peer:
        peer = _Peer(payload or self.health, identity)
        self.addCleanup(peer.close)
        return peer

    def test_every_scheme_casing_reaches_the_pinned_peer_over_verified_tls(self) -> None:
        self.pin_victim()
        peer = self.serve(self.victim)
        for scheme in SCHEMES:
            with self.subTest(scheme=scheme):
                before = peer.requests
                payload = pc._peer_get_json(f"{scheme}://127.0.0.1:{peer.port}{HEALTH_PATH}",
                                            self.settings, peer_id="victim")
                self.assertEqual(payload.get("drone_id"), "victim")
                self.assertEqual(peer.requests, before + 1)

    def test_mixed_case_route_with_wrong_certificate_fails_before_any_request(self) -> None:
        self.pin_victim()
        impostor = self.serve(self.attacker)
        for scheme in SCHEMES:
            with self.subTest(scheme=scheme):
                with self.assertRaises((ssl.SSLError, URLError)):
                    pc._peer_get_json(f"{scheme}://127.0.0.1:{impostor.port}{HEALTH_PATH}",
                                      self.settings, peer_id="victim")
        self.assertEqual(impostor.requests, 0)

    def test_mixed_case_route_without_pin_is_refused_before_dialing(self) -> None:
        peer = self.serve(self.victim)
        for scheme in SCHEMES:
            with self.subTest(scheme=scheme), mock.patch.object(pc, "urlopen") as dial:
                with self.assertRaises(ssl.SSLError):
                    pc._peer_get_json(f"{scheme}://127.0.0.1:{peer.port}{HEALTH_PATH}",
                                      self.settings, peer_id="victim")
                dial.assert_not_called()
        self.assertEqual(peer.requests, 0)

    def test_mixed_case_route_selection_uses_pin_and_never_plaintext(self) -> None:
        self.pin_victim()
        tls_peer = self.serve(self.victim)
        plaintext = _Peer(self.health)
        self.addCleanup(plaintext.close)
        record = {
            "drone_id": "victim",
            "scheme": "HTTPS",
            "paired_over_tls": True,
            "certificate_fingerprint": self.victim_pin,
            "api_port": tls_peer.port,
            "peer_mtls_port": tls_peer.port,
            "reachable_url": f"HTTPS://127.0.0.1:{tls_peer.port}",
            "advertised_reachable_url": f"HTTP://127.0.0.1:{plaintext.port}",
        }
        payload, address = pc._peer_get_json_for_peer(record, HEALTH_PATH, self.settings, peer_id="victim")
        self.assertEqual(payload.get("drone_id"), "victim")
        self.assertEqual(address, f"https://127.0.0.1:{tls_peer.port}")
        self.assertEqual(plaintext.requests, 0)

    def test_mixed_case_plaintext_only_record_does_not_downgrade_to_http_route(self) -> None:
        self.pin_victim()
        plaintext = _Peer(self.health)
        self.addCleanup(plaintext.close)
        record = {
            "drone_id": "victim",
            "scheme": "HtTpS",
            "paired_over_tls": True,
            "certificate_fingerprint": self.victim_pin,
            "api_port": plaintext.port,
            "reachable_url": f"HTTP://127.0.0.1:{plaintext.port}",
        }
        # The only route is cleartext, so no candidate survives the TLS requirement.
        with self.assertRaisesRegex(ValueError, "no peer address available"):
            pc._peer_get_json_for_peer(record, HEALTH_PATH, self.settings, peer_id="victim")
        self.assertEqual(plaintext.requests, 0)

    def test_authenticated_peer_info_accepts_mixed_case_https_only_with_matching_pin(self) -> None:
        for scheme in SCHEMES:
            with self.subTest(scheme=scheme):
                peer = self.serve(self.victim)
                info = pc._fetch_peer_info(f"{scheme}://127.0.0.1:{peer.port}", expected_fingerprint=self.victim_pin)
                self.assertEqual(info.get("drone_id"), "victim")

    def test_authenticated_peer_info_rejects_mixed_case_https_with_wrong_pin(self) -> None:
        impostor = self.serve(self.attacker, payload=dict(self.health, drone_id="victim"))
        for scheme in SCHEMES:
            with self.subTest(scheme=scheme):
                with self.assertRaises((ssl.SSLError, URLError, OSError)):
                    pc._fetch_peer_info(f"{scheme}://127.0.0.1:{impostor.port}",
                                        expected_fingerprint=self.victim_pin)
        self.assertEqual(impostor.requests, 0)

    def test_identity_hold_blocks_mixed_case_https_before_any_socket(self) -> None:
        self.pin_victim()
        peer = self.serve(self.victim)
        with mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=False):
            for scheme in SCHEMES:
                with self.subTest(scheme=scheme):
                    with self.assertRaises(pc.PeerIdentityHold):
                        pc._drone_client_ssl_context(self.settings, f"{scheme}://127.0.0.1:{peer.port}/x", verify=True,
                                                     cafile=Path(self.victim[0]))
                    with mock.patch.object(pc, "urlopen") as dial:
                        with self.assertRaises(pc.PeerIdentityHold):
                            pc._peer_get_json(f"{scheme}://127.0.0.1:{peer.port}{HEALTH_PATH}",
                                              self.settings, peer_id="victim")
                        dial.assert_not_called()
        self.assertEqual(peer.requests, 0)


if __name__ == "__main__":
    unittest.main()
