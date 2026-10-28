"""Pinned-HTTPS peer POSTs must reach the paired peer (membership removal, NFS revoke).

Regression: ``_peer_post_json`` called ``HTTPSConnection.request(..., data=...)``, which
is not a valid keyword, so every cert-pinned POST raised ``TypeError`` before any byte
was sent. Removal gossip, NFS-export revocation and the other security-relevant POSTs
then never reached the pinned peer (callers such as ``_revoke_nfs_export`` swallow it).
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

from app.common.settings import Settings
from app.transfer import peer_connectivity as pc


class _Peer:
    def __init__(self, cert: Path, key: Path, status: int = 200) -> None:
        self.requests: list[dict] = []
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                size = int(self.headers.get("Content-Length") or 0)
                peer.requests.append(json.loads(self.rfile.read(size).decode("utf-8")))
                raw = b'{"revoked":true}'
                self.send_response(status)
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
class PinnedPostDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.keys = tempfile.TemporaryDirectory(prefix="issue-603-post-")
        cls.cert, cls.key = Path(cls.keys.name, "p.crt"), Path(cls.keys.name, "p.key")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(cls.key),
             "-out", str(cls.cert), "-days", "1", "-subj", "/CN=paired",
             "-addext", "subjectAltName=IP:127.0.0.1"],
            check=True, capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.keys.cleanup()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="issue-603-state-")
        self.addCleanup(tmp.cleanup)
        with mock.patch.dict(os.environ, {"USERDATA_ROOT": tmp.name, "DRONE_DEVICE_ID": "me"}, clear=True):
            self.settings = Settings.from_env()
        patch = mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=True)
        patch.start()
        self.addCleanup(patch.stop)
        pc._save_local_peer_certificate(self.settings, "victim", self.cert.read_text(encoding="utf-8"))

    def test_pinned_https_post_body_is_delivered_to_the_pinned_peer(self) -> None:
        peer = _Peer(self.cert, self.key)
        self.addCleanup(peer.close)
        body = pc._peer_post_json(
            f"https://127.0.0.1:{peer.port}/v1/api/peer/network-share/nfs/revoke",
            {"protocol_version": 1}, self.settings, peer_id="victim",
        )
        self.assertEqual(body, {"revoked": True})
        self.assertEqual(peer.requests, [{"protocol_version": 1}])

    def test_pinned_https_post_error_status_is_an_http_error_not_a_type_error(self) -> None:
        from urllib.error import HTTPError

        peer = _Peer(self.cert, self.key, status=409)
        self.addCleanup(peer.close)
        with self.assertRaises(HTTPError) as caught:
            pc._peer_post_json(
                f"https://127.0.0.1:{peer.port}/v1/api/peer/membership",
                {"records": []}, self.settings, peer_id="victim",
            )
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(peer.requests, [{"records": []}])


if __name__ == "__main__":
    unittest.main()
