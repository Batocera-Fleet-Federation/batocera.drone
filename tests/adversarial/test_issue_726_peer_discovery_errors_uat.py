"""Issue #726 UAT: Peer save discovery errors must stay specific over real TLS sockets.

Trusted amendment quote:
"When I click on \"show saves\" for BATOCERA drone it says: Could not read saves from
this Drone. It may be offline or need an update.  However, it is online and I can
search assets from that machine (so i know it is online).  This needs to be fixed."
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common.settings import Settings
from app.transfer import game_saves as saves
from app.transfer import local_network
from app.web.handlers_game_saves import HandlersGameSavesMixin


def _generate_self_signed_cert(cert_path: Path, key_path: Path, cn: str) -> None:
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-nodes",
            "-subj",
            f"/CN={cn}",
        ],
        check=True,
        capture_output=True,
    )


class _SilentHttpServer(HTTPServer):
    def handle_error(self, request, client_address):
        # Expected TLS handshake rejections / client aborts in negative tests.
        return


class PeerDiscoveryErrorsUat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(saves._CHOICES.clear)

        env = {
            "USERDATA_ROOT": str(self.root),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.db"),
            "DRONE_DEVICE_ID": "local-drone",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.settings = Settings.from_env()

        self.srv_cert = self.root / "srv.crt"
        self.srv_key = self.root / "srv.key"
        self.cli_cert = self.root / "cli.crt"
        self.cli_key = self.root / "cli.key"
        self.other_cert = self.root / "other.crt"
        self.other_key = self.root / "other.key"
        _generate_self_signed_cert(self.srv_cert, self.srv_key, "batocera-peer")
        _generate_self_signed_cert(self.cli_cert, self.cli_key, "local-drone")
        _generate_self_signed_cert(self.other_cert, self.other_key, "other-ca")

        self.settings = SimpleNamespace(
            saves_root=self.settings.saves_root,
            userdata_root=self.settings.userdata_root,
            device_id=self.settings.device_id,
            drone_mtls_enabled=True,
            drone_cert_file=self.cli_cert,
            drone_key_file=self.cli_key,
        )
        cert_dir = self.root / "system" / "drone-app" / "local-peer-certs"
        cert_dir.mkdir(parents=True)
        srv_pem = self.srv_cert.read_text(encoding="utf-8")
        (cert_dir / "batocera.crt").write_text(srv_pem, encoding="utf-8")
        self.peer = {
            "drone_id": "batocera",
            "name": "BATOCERA",
            "certificate_fingerprint": saves._certificate_pem_fingerprint(srv_pem),
            "paired_over_tls": True,
        }
        self.item = {
            "system": "snes",
            "rom": "Mario.sfc",
            "stem": "Mario",
            "filename": "Mario.sfc",
            "ids": [],
            "fingerprint": "",
        }

    def _start_tls_server(self, handler_cls, *, require_ca: Path | None = None):
        httpd = _SilentHttpServer(("127.0.0.1", 0), handler_cls)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(self.srv_cert), str(self.srv_key))
        if require_ca is not None:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cafile=str(require_ca))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return port

    def _run_handler_discover(self, port: int) -> tuple[int, dict]:
        handler = HandlersGameSavesMixin()
        handler.settings = self.settings
        handler._save_game = mock.Mock(return_value=self.item)
        sent = {}

        def capture(status, payload):
            sent["status"] = status
            sent["payload"] = payload

        handler._send_json = capture
        with mock.patch.object(local_network, "get_paired_peer", return_value=self.peer), mock.patch.object(
            saves, "_peer_address_candidates", return_value=[f"https://127.0.0.1:{port}"]
        ):
            handler._handle_game_saves_get("snes", "rom-1", {"drone": ["batocera"]})
        return sent["status"], sent["payload"]

    def test_slow_peer_timeout_reports_timeout_not_unavailable(self):
        """Real slow-peer timeout over TLS must surface as 504 timeout, not 502 unavailable."""
        release = threading.Event()
        self.addCleanup(release.set)

        class SlowHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                release.wait(2.0)

            def log_message(self, *args):
                return

        port = self._start_tls_server(SlowHandler)
        with mock.patch.object(saves, "DISCOVERY_TIMEOUT", 0.15):
            status, payload = self._run_handler_discover(port)
        self.assertEqual(status, 504, payload)
        self.assertIn("took too long", payload["error"].lower())

    def test_tls_rejection_reports_certificate_failure_not_unavailable(self):
        """Real mTLS client-cert rejection must report TLS/cert failure, not generic unavailable."""

        class OkHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                return

        port = self._start_tls_server(OkHandler, require_ca=self.other_cert)
        status, payload = self._run_handler_discover(port)
        self.assertEqual(status, 502, payload)
        msg = payload["error"].lower()
        self.assertTrue(
            "certificate" in msg or "secure connection" in msg or "re-pair" in msg,
            f"Expected TLS/certificate error, got: {payload}",
        )

    def test_unreachable_peer_reports_offline_via_discovery_failure(self):
        """Unreachable socket must produce the specific offline/unreachable message."""
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            dead_port = sock.getsockname()[1]
        status, payload = self._run_handler_discover(dead_port)
        self.assertEqual(status, 502, payload)
        msg = payload["error"].lower()
        self.assertTrue("offline" in msg or "unreachable" in msg, payload)

    def test_endpoint_404_json_error_does_not_falsely_claim_peer_needs_update(self):
        """A peer whose /v1/api/peer/game-saves endpoint exists and returns JSON 404
        (e.g. ROM missing) must not tell the user the peer needs a software update."""

        class MissingRomHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"error": "ROM or save not found."}).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        port = self._start_tls_server(MissingRomHandler)
        status, payload = self._run_handler_discover(port)
        self.assertNotIn("does not support save sharing", payload["error"].lower())


if __name__ == "__main__":
    unittest.main()
