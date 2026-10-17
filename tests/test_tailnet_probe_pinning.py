"""Paired tailnet route refreshes require proof of the pinned identity (#337)."""

import hashlib
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

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity
from app.web import handlers_network


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class TailnetProbePinningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with mock.patch.dict(os.environ, {"USERDATA_ROOT": str(self.root),
                                         "DRONE_DEVICE_ID": "local"}, clear=True):
            self.settings = Settings.from_env()
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = self.settings
        self.handler._repair_one_sided_pairing = mock.Mock(side_effect=lambda peer: peer)
        self.cert, self.key, self.pin = self.certificate("victim")
        self.original = local_network.save_paired_peer(self.settings, {
            "drone_id": "victim", "certificate_fingerprint": self.pin,
            "reachable_url": "https://192.168.1.20", "api_port": 443,
            "peer_mtls_port": 8543, "tailnet_ip": "100.64.0.9",
        })
        self.info = {"service": local_network.DISCOVERY_SERVICE, "drone_id": "victim",
                     "api_port": 9999, "peer_mtls_port": 9999, "scheme": "https"}

    def certificate(self, name):
        cert, key = self.root / (name + ".crt"), self.root / (name + ".key")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "1",
                        "-subj", "/CN=" + name], check=True, capture_output=True)
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        return cert, key, pin

    def serve(self, cert, key, payload, status=200):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Location", "http://127.0.0.1:1/spoofed")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(cleanup)
        return "https://127.0.0.1:" + str(server.server_port)

    def sync(self, address, bootstrap):
        # Use a local listener in place of a tailnet IP, retaining the real probe,
        # TLS verifier and persistent paired-peer store.
        return self.handler._sync_tailnet_device({"tailnet_ip": address}, info=bootstrap)

    def assert_routes_unchanged(self):
        stored = local_network.get_paired_peer(self.settings, "victim")
        for field in ("reachable_url", "tailnet_ip", "api_port", "peer_mtls_port",
                      "certificate_fingerprint", "advertised_reachable_url"):
            self.assertEqual(stored.get(field), self.original.get(field), field)
        self.handler._repair_one_sided_pairing.assert_not_called()

    def test_attacker_cannot_repoint_with_missing_or_copied_body_fingerprint(self):
        cert, key, _ = self.certificate("attacker")
        for fingerprint in (None, self.pin):
            with self.subTest(fingerprint=fingerprint):
                info = dict(self.info)
                if fingerprint:
                    info["certificate_fingerprint"] = fingerprint
                address = self.serve(cert, key, info)
                result = self.sync(address, info)
                self.assertIn("tailnet_identity_error", result)
                self.assert_routes_unchanged()

    def test_copied_public_identity_in_attacker_chain_cannot_refresh(self):
        cert, key, _ = self.certificate("attacker-chain")
        cert.write_text(cert.read_text() + self.cert.read_text())
        address = self.serve(cert, key, {**self.info, "certificate_fingerprint": self.pin})
        self.sync(address, self.info)
        self.assert_routes_unchanged()

    def test_authenticated_response_repairs_route_and_ports_without_body_fingerprint(self):
        address = self.serve(self.cert, self.key, self.info)
        self.sync(address, {**self.info, "api_port": 6666, "peer_mtls_port": 6666})
        stored = local_network.get_paired_peer(self.settings, "victim")
        self.assertEqual(stored["tailnet_ip"], address)
        self.assertEqual(stored["api_port"], 9999)
        self.assertEqual(stored["peer_mtls_port"], 9999)
        self.assertEqual(stored["reachable_url"], self.original["reachable_url"])
        self.assertEqual(stored["certificate_fingerprint"], self.pin)

    def test_https_leaf_signed_by_pinned_identity_can_refresh(self):
        from app.web.server_tls import _generate_server_leaf

        cert, key = self.root / "leaf.crt", self.root / "leaf.key"
        _generate_server_leaf(self.settings, cert, key, self.cert, self.key, ["IP:127.0.0.1"])
        address = self.serve(cert, key, self.info)
        result = self.sync(address, self.info)
        self.assertNotIn("tailnet_identity_error", result)
        self.assertEqual(local_network.get_paired_peer(self.settings, "victim")["peer_mtls_port"], 9999)

    def test_authenticated_response_with_different_drone_id_cannot_refresh(self):
        address = self.serve(self.cert, self.key, {**self.info, "drone_id": "other"})
        self.sync(address, self.info)
        self.assert_routes_unchanged()

    def test_redirect_cannot_bypass_tls_verification(self):
        address = self.serve(self.cert, self.key, self.info, status=302)
        self.sync(address, self.info)
        self.assert_routes_unchanged()

    def test_missing_pin_and_plaintext_are_rejected_before_dial(self):
        for address, pin in (("http://127.0.0.1", self.pin), ("https://127.0.0.1", "")):
            with self.subTest(address=address, pin=pin), \
                    mock.patch.object(peer_connectivity, "_pairing_tls_context") as context:
                with self.assertRaises(ssl.SSLError):
                    peer_connectivity._fetch_peer_info(address, expected_fingerprint=pin)
                context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
