"""Local-only regressions for gossip's TLS pin and rejected-update boundaries."""

import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import urllib.request
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity, swarm_membership
from app.transfer.drone_tls import DroneCertificateManager


class GossipPinBoundaryTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl is required")
        temporary = tempfile.TemporaryDirectory(prefix="issue246-pin-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = self._settings("local")
        victim = self._settings("victim")
        certificate = DroneCertificateManager(victim).ensure_certificate()
        self.pem = certificate["public_certificate"]
        self.path, self.fingerprint = peer_connectivity._save_local_peer_certificate(
            self.settings, "victim", self.pem
        )
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)
        local_network.save_paired_peer(self.settings, {
            "drone_id": "victim", "scheme": "https", "api_port": 443,
            "peer_mtls_port": 443, "reachable_url": "https://127.0.0.1:1",
            "certificate_fingerprint": self.fingerprint,
            "certificate_path": str(self.path), "paired_over_tls": True,
        })
        local_network.save_paired_peer(self.settings, {
            "drone_id": "introducer", "reachable_url": "https://127.0.0.1:2",
        })

    def _settings(self, identity):
        root = self.root / identity
        with mock.patch.dict(os.environ, {
            "USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": identity,
            "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        }, clear=True):
            return Settings.from_env()

    def _gossip(self, scheme, url, port):
        # Hop 3 prevents forwarding; the public authenticated membership entry
        # point still parses and applies the supplied record normally.
        return swarm_membership.handle_membership_request(self.settings, {
            "hop": 3,
            "records": [{
                "peer_id": "victim", "op": "add", "epoch": 100,
                "origin": "introducer", "card": {
                    "scheme": scheme, "reachable_url": url,
                    "api_port": port, "peer_mtls_port": port,
                    "certificate_pem": self.pem,
                    "certificate_fingerprint": self.fingerprint,
                },
            }],
        }, "introducer")

    def test_rejected_http_gossip_preserves_live_victim_certificate(self):
        original = self.path.read_bytes()
        self._gossip("http", "http://127.0.0.1:8080", 8080)
        peer = local_network.get_paired_peer(self.settings, "victim")
        self.assertEqual(peer["scheme"], "https")
        self.assertTrue(self.path.exists(), "Rejected gossip deleted the live victim pin")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(peer_connectivity._peer_trust_cafile(self.settings, "victim"), self.path)

    def test_uppercase_https_gossip_cannot_bypass_victim_certificate_pin(self):
        cert, key = self.root / "attacker.crt", self.root / "attacker.key"
        subprocess.run([
            "openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
        ], check=True, capture_output=True, timeout=30)
        hits = []

        class Attacker(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                hits.append(self.path)
                body = json.dumps({"forged_inventory": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Attacker)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        self._gossip("https", f"HTTPS://127.0.0.1:{port}", port)
        peer = local_network.get_paired_peer(self.settings, "victim")
        self.assertEqual(peer["certificate_fingerprint"], self.fingerprint)
        # This local trust root models an attacker with an ordinary publicly
        # trusted certificate. It is deliberately different from victim's pin.
        # Verification remains enabled; only the platform default trust store
        # is replaced with this local fixture root.
        with mock.patch.dict(os.environ, {
            "SSL_CERT_FILE": str(cert), "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }), mock.patch.object(ssl, "_create_default_https_context",
                              side_effect=lambda **kw: ssl.create_default_context(cafile=str(cert), **kw)), \
                mock.patch.object(urllib.request, "_opener", None), \
                mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True):
            # Control: lowercase HTTPS rejects exactly the same attacker cert.
            with self.assertRaises((ssl.SSLError, URLError)):
                peer_connectivity._peer_get_json(
                    f"https://127.0.0.1:{port}/peer/inventory", self.settings,
                    peer_id="victim", timeout=2,
                )
            try:
                peer_connectivity._peer_get_json_for_peer(
                    peer, "/peer/inventory", self.settings, peer_id="victim", timeout=2,
                )
            except (ssl.SSLError, URLError, ValueError):
                pass
        self.assertEqual(hits, [], "A server without victim's key received and answered pinned peer traffic")


if __name__ == "__main__":
    unittest.main()
