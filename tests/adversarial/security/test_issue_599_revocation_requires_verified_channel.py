"""#599: a peer 404 revokes shared credentials only when received on the pinned TLS connection.

Route strings, HTTPError.url and recorded schemes are attacker-influenced
metadata (a plaintext on-path attacker can redirect to any URL), so they must
never stand in for the verified connection. Everything here is loopback-only.
"""

import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from app.common.settings import Settings
from app.device import vpn_manager as vpn
from app.transfer import peer_connectivity as pc

SOURCE = "source-599"


class _Server:
    def __init__(self, status, location=None, tls=None):
        self.hits = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.hits.append(self.path)
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class RevocationRequiresVerifiedChannelSecurityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cert, self.key = self.root / "c.pem", self.root / "k.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(self.key),
                        "-out", str(self.cert), "-days", "1", "-subj", "/CN=source"],
                       check=True, capture_output=True)
        env = {"USERDATA_ROOT": str(self.root), "ROMS_ROOT": str(self.root / "roms"),
               "BIOS_ROOT": str(self.root / "bios"), "SAVES_ROOT": str(self.root / "saves"),
               "DRONE_STATE_DATABASE_FILE": str(self.root / "state.sqlite3"),
               "DRONE_DEVICE_ID": "victim-599", "DRONE_VPN_DIR": str(self.root / "vpn")}
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        for patcher in (mock.patch.object(vpn, "_drone_install_root", return_value=self.root),
                        mock.patch.object(vpn, "find_openvpn_binary", return_value=None),
                        mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=True),
                        mock.patch.object(pc, "_candidate_dialable", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        vpn.import_from_peer(self.settings, {
            "config_filename": "client.ovpn",
            "config_text": "client\ndev tun\nremote vpn.example 1194\nauth-user-pass\n",
            "has_credentials": True, "username": "u", "password": "secret"}, source_peer_id=SOURCE)
        pc._save_local_peer_certificate(self.settings, SOURCE, self.cert.read_text())

    def _serve(self, *args, **kwargs):
        server = _Server(*args, **kwargs)
        self.addCleanup(server.close)
        return server

    def _check(self, peer):
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                mock.patch.object(vpn, "disconnect"):
            return vpn.check_sharing_revocation(self.settings)

    def _tls_peer(self, port):
        return {"drone_id": SOURCE, "scheme": "https", "protocol": "https", "paired_over_tls": True,
                "local_ip": "127.0.0.1", "api_port": port, "peer_mtls_port": port}

    def test_route_stamp_and_https_error_url_are_not_evidence(self):
        # Mixed-case/plain stamps and https URLs are metadata, not a verified channel.
        for url, route in (("https://source/x", "https://127.0.0.1:443"),
                           ("HTTP://source/x", "HTTPS://127.0.0.1:443")):
            with self.subTest(url=url, route=route):
                error = HTTPError(url, 404, "not found", None, None)
                error.peer_route = route
                self.assertFalse(pc.peer_http_error_is_authenticated(error))
                with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE}), \
                        mock.patch.object(pc, "_peer_get_json_for_peer", side_effect=error), \
                        mock.patch.object(vpn, "disconnect") as disconnect:
                    self.assertFalse(vpn.check_sharing_revocation(self.settings))
                disconnect.assert_not_called()
                self.assertTrue(vpn.auth_path(self.settings).is_file())

    def test_pinned_tls_redirect_to_plaintext_404_cannot_revoke(self):
        attacker = self._serve(404)
        tls = self._serve(302, location=f"http://127.0.0.1:{attacker.port}/v1/api/peer/vpn/config",
                          tls=(str(self.cert), str(self.key)))
        self.assertFalse(self._check(self._tls_peer(tls.port)))
        self.assertEqual(attacker.hits, [], "redirect from the TLS peer must not be followed to plaintext")
        self.assertTrue(vpn.auth_path(self.settings).is_file())

    def test_plaintext_404_with_cached_pin_cannot_revoke(self):
        attacker = self._serve(404)
        peer = {"drone_id": SOURCE, "scheme": "http", "paired_over_tls": False,
                "local_ip": "127.0.0.1", "api_port": attacker.port}
        self.assertFalse(self._check(peer))
        self.assertTrue(attacker.hits)
        self.assertTrue(vpn.auth_path(self.settings).is_file())

    def test_pinned_tls_404_still_revokes(self):
        tls = self._serve(404, tls=(str(self.cert), str(self.key)))
        self.assertTrue(self._check(self._tls_peer(tls.port)))
        self.assertFalse(vpn.auth_path(self.settings).is_file())


if __name__ == "__main__":
    unittest.main()
