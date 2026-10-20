"""Mixed-case HTTPS keeps the peer pin for worker-facing binary downloads."""

from __future__ import annotations

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


class _Peer:
    def __init__(self, certificate: tuple[Path, Path], body: bytes) -> None:
        self.requests = 0
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                peer.requests += 1
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certificate[0]), str(certificate[1]))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = int(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class MixedCaseBinaryDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="issue-332-identities-")
        root = Path(cls._tmp.name)
        cls.identities = {}
        for name in ("paired", "impostor"):
            cert, key = root / f"{name}.crt", root / f"{name}.key"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(cert), "-days", "1",
                 "-subj", f"/CN={name}"],
                check=True, capture_output=True,
            )
            cls.identities[name] = (cert, key)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="issue-332-download-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        with mock.patch.dict(os.environ, {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "receiver"}, clear=True):
            self.settings = Settings.from_env()
        allow = mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=True)
        allow.start()
        self.addCleanup(allow.stop)
        self.pin = pc._certificate_pem_fingerprint(self.identities["paired"][0].read_text())

    def serve(self, identity: str, body: bytes) -> _Peer:
        peer = _Peer(self.identities[identity], body)
        self.addCleanup(peer.close)
        return peer

    def test_uppercase_and_mixed_case_download_uses_pinned_tls_socket(self) -> None:
        content = b"signed-upstream-attachment\x00\xff"
        peer = self.serve("paired", content)
        pc._save_local_peer_certificate(self.settings, "satellite", self.identities["paired"][0].read_text())
        record = {
            "drone_id": "satellite", "scheme": "HTTPS", "paired_over_tls": True,
            "certificate_fingerprint": self.pin,
            "reachable_url": f"HTTPS://127.0.0.1:{peer.port}",
        }
        destination = Path(self.settings.userdata_root) / "attachment.bin"

        size, address = pc._peer_download_file_for_peer(
            record, "/peer/attachment", destination, self.settings, peer_id="satellite",
        )

        self.assertEqual(size, len(content))
        self.assertEqual(destination.read_bytes(), content)
        self.assertEqual(address, f"https://127.0.0.1:{peer.port}")
        self.assertEqual(peer.requests, 1)

    def test_wrong_identity_cannot_serve_download_and_leaves_no_partial_file(self) -> None:
        peer = self.serve("impostor", b"attacker bytes")
        pc._save_local_peer_certificate(self.settings, "satellite", self.identities["paired"][0].read_text())
        record = {
            "drone_id": "satellite", "scheme": "hTtPs", "paired_over_tls": True,
            "certificate_fingerprint": self.pin,
            "reachable_url": f"HtTpS://127.0.0.1:{peer.port}",
        }
        destination = Path(self.settings.userdata_root) / "attachment.bin"

        with self.assertRaises((ssl.SSLError, URLError, OSError)):
            pc._peer_download_file_for_peer(
                record, "/peer/attachment", destination, self.settings, peer_id="satellite",
            )

        self.assertEqual(peer.requests, 0)
        self.assertFalse(destination.exists())

    def test_tls_paired_peer_with_only_http_route_is_not_dialed(self) -> None:
        peer = self.serve("paired", b"must not be served")
        record = {
            "drone_id": "satellite", "scheme": "HTTPS", "paired_over_tls": True,
            "certificate_fingerprint": self.pin,
            "reachable_url": f"HTTP://127.0.0.1:{peer.port}",
        }
        destination = Path(self.settings.userdata_root) / "attachment.bin"
        with self.assertRaisesRegex(ValueError, "no peer address available"):
            pc._peer_download_file_for_peer(
                record, "/peer/attachment", destination, self.settings, peer_id="satellite",
            )
        self.assertEqual(peer.requests, 0)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
