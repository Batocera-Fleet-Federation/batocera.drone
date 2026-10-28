"""Issue #630 regression: a TLS redirect to plaintext 404 is not revocation."""

from __future__ import annotations

import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.common.settings import Settings
from app.transfer import peer_connectivity as pc

SOURCE = "source-630"
OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"


def _settings(root: Path) -> Settings:
    with mock.patch.dict("os.environ", {
        "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "adversarial-630",
    }, clear=True):
        return Settings.from_env()


class _Plain404:
    def __init__(self) -> None:
        self.hits = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner.hits += 1
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class _TlsRedirect:
    def __init__(self, cert: Path, key: Path, target: str) -> None:
        self.hits = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner.hits += 1
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class Issue630PinnedHttpsRedirectPlaintext404(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="issue-630-cert-")
        root = Path(cls.tmp.name)
        cls.cert, cls.key = root / "peer.crt", root / "peer.key"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(cls.key), "-out", str(cls.cert), "-days", "1",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        ], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_redirected_plaintext_404_does_not_revoke_or_reach_http_hop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="issue-630-state-") as tmp:
            root = Path(tmp)
            settings = _settings(root)
            with mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install"), \
                    mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=True):
                pc._save_local_peer_certificate(settings, SOURCE, self.cert.read_text(encoding="utf-8"))
                vpn_manager.import_from_peer(settings, {
                    "config_filename": "client.ovpn", "config_text": OVPN,
                    "has_credentials": True, "username": "vpn-user", "password": "vpn-pass-630",
                }, source_peer_id=SOURCE, source_peer_name="TLS Source")
                plain = _Plain404()
                target = f"http://127.0.0.1:{plain.port}/v1/api/peer/vpn/config"
                tls = _TlsRedirect(self.cert, self.key, target)
                try:
                    peer = {
                        "drone_id": SOURCE, "scheme": "https", "protocol": "https",
                        "paired_over_tls": True, "local_ip": "127.0.0.1",
                        "api_port": tls.port, "peer_mtls_port": tls.port,
                    }
                    with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                            mock.patch.object(vpn_manager, "disconnect") as disconnect:
                        self.assertFalse(vpn_manager.check_sharing_revocation(settings))
                    disconnect.assert_not_called()
                    state = vpn_manager._load_state(settings)
                    self.assertTrue(state["has_credentials"])
                    self.assertEqual(state["revoked_reason"], "")
                    self.assertEqual(vpn_manager.auth_path(settings).read_text(), "vpn-user\nvpn-pass-630\n")
                    self.assertEqual(tls.hits, 1)
                    self.assertEqual(plain.hits, 0)
                finally:
                    tls.close()
                    plain.close()


if __name__ == "__main__":
    unittest.main()
