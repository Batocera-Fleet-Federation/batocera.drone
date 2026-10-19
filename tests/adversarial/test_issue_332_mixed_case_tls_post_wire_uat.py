"""Real-wire POST coverage for #332's mixed-case HTTPS pinning boundary."""

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
from app.transfer import peer_connectivity as pc


class _TlsPostPeer:
    def __init__(self, cert: Path, key: Path) -> None:
        self.requests: list[dict] = []
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                size = int(self.headers.get("Content-Length") or 0)
                peer.requests.append(json.loads(self.rfile.read(size).decode("utf-8")))
                raw = b'{"accepted":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = int(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class MixedCaseHttpsPostWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.keys = tempfile.TemporaryDirectory(prefix="issue-332-post-identities-")
        root = Path(cls.keys.name)
        cls.identities: dict[str, tuple[Path, Path]] = {}
        for name in ("paired", "impostor"):
            cert, key = root / f"{name}.crt", root / f"{name}.key"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                 "-out", str(cert), "-days", "1", "-subj", f"/CN={name}",
                 "-addext", "subjectAltName=IP:127.0.0.1"],
                check=True, capture_output=True,
            )
            cls.identities[name] = (cert, key)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.keys.cleanup()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="issue-332-post-")
        self.addCleanup(tmp.cleanup)
        with mock.patch.dict(os.environ, {"USERDATA_ROOT": tmp.name, "DRONE_DEVICE_ID": "receiver"}, clear=True):
            self.settings = Settings.from_env()
        allowed = mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=True)
        allowed.start()
        self.addCleanup(allowed.stop)

    def serve(self, identity: str) -> _TlsPostPeer:
        peer = _TlsPostPeer(*self.identities[identity])
        self.addCleanup(peer.close)
        return peer

    def test_mixed_case_https_post_reaches_only_the_pinned_peer(self) -> None:
        certificate, _ = self.identities["paired"]
        pc._save_local_peer_certificate(self.settings, "victim", certificate.read_text(encoding="utf-8"))
        peer = self.serve("paired")
        for scheme in ("HTTPS", "Https", "hTTps", "https"):
            with self.subTest(scheme=scheme):
                self.assertEqual(
                    pc._peer_post_json(
                        f"{scheme}://127.0.0.1:{peer.port}/v1/api/peer/action",
                        {"scheme": scheme}, self.settings, peer_id="victim",
                    ),
                    {"accepted": True},
                )
        self.assertEqual(peer.requests, [{"scheme": value} for value in ("HTTPS", "Https", "hTTps", "https")])

    def test_mixed_case_https_post_rejects_an_impostor_before_handler_execution(self) -> None:
        certificate, _ = self.identities["paired"]
        pc._save_local_peer_certificate(self.settings, "victim", certificate.read_text(encoding="utf-8"))
        impostor = self.serve("impostor")
        for scheme in ("HTTPS", "hTTps"):
            with self.subTest(scheme=scheme):
                with self.assertRaises((ssl.SSLError, URLError, OSError)):
                    pc._peer_post_json(
                        f"{scheme}://127.0.0.1:{impostor.port}/v1/api/peer/action",
                        {"must": "not reach impostor"}, self.settings, peer_id="victim",
                    )
        self.assertEqual(impostor.requests, [])


if __name__ == "__main__":
    unittest.main()
