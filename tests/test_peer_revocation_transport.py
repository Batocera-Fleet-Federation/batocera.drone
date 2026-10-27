"""Revocation status must belong to the connection verified by the peer pin."""
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
from app.transfer import peer_connectivity as pc


class RevocationTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.cert, cls.key = cls.root / 'cert.pem', cls.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(cls.key), '-out', str(cls.cert), '-days', '1',
                        '-subj', '/CN=peer'], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with mock.patch.dict('os.environ', {'USERDATA_ROOT': str(self.root),
                                          'DRONE_DEVICE_ID': 'revocation-test'}, clear=True):
            self.settings = Settings.from_env()
        patch = mock.patch.object(pc, '_identity_swarm_networking_allowed', return_value=True)
        patch.start()
        self.addCleanup(patch.stop)
        pc._save_local_peer_certificate(self.settings, 'peer', self.cert.read_text())

    def serve(self, status, tls=False, location=None):
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(status)
                if location:
                    self.send_header('Location', location)
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(self.cert), str(self.key))
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        return f"{'https' if tls else 'http'}://127.0.0.1:{server.server_port}", hits

    def test_pinned_tls_success_preserves_path_and_query(self):
        url, hits = self.serve(200, tls=True)
        self.assertEqual(pc._peer_get_json(url + '/config?nonce=abc', self.settings, peer_id='peer'), {})
        self.assertEqual(hits, ['/config?nonce=abc'])

    def test_pinned_tls_404_has_connection_evidence(self):
        url, hits = self.serve(404, tls=True)
        with self.assertRaises(HTTPError) as caught:
            pc._peer_get_json(url + '/config', self.settings, peer_id='peer')
        self.assertTrue(pc.peer_http_error_is_authenticated(caught.exception))
        self.assertEqual(hits, ['/config'])

    def test_pinned_tls_redirect_cannot_turn_plaintext_404_into_revocation(self):
        terminal, hits = self.serve(404)
        url, _ = self.serve(302, tls=True, location=terminal + '/config')
        with self.assertRaises(HTTPError) as caught:
            pc._peer_get_json(url + '/config', self.settings, peer_id='peer')
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(hits, [])

    def test_plaintext_404_with_cached_pin_has_no_connection_evidence(self):
        url, _ = self.serve(404)
        with self.assertRaises(HTTPError) as caught:
            pc._peer_get_json(url + '/config', self.settings, peer_id='peer')
        caught.exception.peer_route = 'https://peer'
        self.assertFalse(pc.peer_http_error_is_authenticated(caught.exception))

    def test_wrong_pin_cannot_produce_authenticated_status(self):
        url, hits = self.serve(404, tls=True)
        # System roots do not trust the test peer's self-signed certificate.
        with mock.patch.object(pc, '_drone_client_ssl_context', return_value=ssl.create_default_context()):
            with self.assertRaises((ssl.SSLError, OSError)):
                pc._peer_get_json(url + '/config', self.settings, peer_id='peer')
        self.assertEqual(hits, [])
