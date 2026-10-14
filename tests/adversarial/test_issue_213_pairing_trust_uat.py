"""Issue #213: pairing secrets need an independent, enforced trust anchor.

Expectations come from the issue's remediation and confidentiality invariant:
unauthenticated discovery cannot choose a cleartext exception; malformed operator
input cannot turn verification off; rejected pairing cannot destroy a live pin;
and the fingerprint on a real Drone's pairing card must permit legitimate TLS
pairing with its identity-signed HTTPS leaf. Servers bind loopback only, all state
is temporary, and neither discovery workers nor external services are started.
"""
from __future__ import annotations

import json
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity, swarm_membership
from app.transfer.drone_tls import DroneCertificateManager
from app.web import handlers_network, server_tls

CODE = "12345678"


def settings_at(root: Path, device_id: str) -> Settings:
    env = {
        "USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": device_id,
        "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "TLS_SELF_SIGNED_DIR": str(root / "https"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "HTTP_ONLY": "0",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class PairingTrustUAT(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.material = tempfile.TemporaryDirectory(prefix="issue-213-material-")
        cls.addClassCleanup(cls.material.cleanup)
        root = Path(cls.material.name)
        cls.remote_settings = settings_at(root / "target", "target-213")
        cls.remote = DroneCertificateManager(cls.remote_settings).ensure_certificate()
        cls.attacker_settings = settings_at(root / "attacker", "attacker-213")
        cls.attacker = DroneCertificateManager(cls.attacker_settings).ensure_certificate()
        cls.leaf_cert, cls.leaf_key = server_tls._resolve_tls_material(cls.remote_settings)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="issue-213-client-")
        self.addCleanup(temp.cleanup)
        self.settings = settings_at(Path(temp.name), "client-213")
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = self.settings
        self.handler.server = mock.Mock()
        self.handler._send_json = mock.Mock()
        # Isolate machine hardware verdict and unrelated membership fan-out,
        # but never mock urlopen, certificate hashing, TLS, or persistent pins.
        for patch in (
            mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True),
            mock.patch.object(local_network, "get_tailnet_ip", return_value=""),
            mock.patch.object(swarm_membership, "propagate_membership"),
            mock.patch.object(handlers_network, "load_peer_cert_everywhere"),
            mock.patch.dict("os.environ", {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def serve(self, *, tls=True, leaf=False, attacker=False, response_updates=None):
        material = self.attacker if attacker else self.remote
        remote_settings = self.attacker_settings if attacker else self.remote_settings
        received = []
        result = {
            "status": "paired", "drone_id": "target-213",
            "certificate_pem": material["public_certificate"],
            "certificate_fingerprint": material["fingerprint"],
            "scheme": "https", "api_port": 443, "peer_mtls_port": 8443,
            **(response_updates or {}),
        }

        class Endpoint(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                received.append(json.loads(body))
                encoded = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):
                encoded = json.dumps({"service": local_network.DISCOVERY_SERVICE, "drone_id": "target-213", "certificate_fingerprint": material["fingerprint"]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            cert, key = ((self.leaf_cert, self.leaf_key) if leaf else
                         (remote_settings.drone_cert_file, remote_settings.drone_key_file))
            context.load_cert_chain(str(cert), str(key))
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "fixture server did not stop")

        self.addCleanup(close)
        address = f"{'https' if tls else 'http'}://127.0.0.1:{server.server_port}"
        peer = {"drone_id": "target-213", "reachable_url": address,
                "certificate_fingerprint": material["fingerprint"]}
        return peer, received

    def discover(self, peer):
        from urllib.parse import urlparse
        parsed = urlparse(peer["reachable_url"])
        return local_network.record_discovered_peer(self.settings, {
            "service": local_network.DISCOVERY_SERVICE, "drone_id": "target-213",
            "scheme": parsed.scheme, "api_port": parsed.port,
            "certificate_fingerprint": peer["certificate_fingerprint"],
        }, "127.0.0.1")

    def attempt(self, peer, fingerprint="", *, direct=False, by_address=False):
        # A rejection may be returned by the admin handler or raised by the
        # pairing primitive. In either case assert observable confidentiality.
        try:
            if direct:
                return peer_connectivity._local_pair_peer(self.settings, peer, CODE,
                                                         confirmed_fingerprint=fingerprint)
            payload = {"pairing_code": CODE, "certificate_fingerprint": fingerprint}
            if by_address:
                self.handler._handle_admin_local_peer_pair_by_address({**payload, "address": peer["reachable_url"]})
            else:
                self.handler._handle_admin_local_peer_pair("target-213", payload)
        except (ValueError, RuntimeError, OSError):
            return None

    def assert_secret_not_sent(self, received):
        self.assertEqual(len(received), 0, "untrusted endpoint received pairing request including code/certificate")
        self.assertIsNone(local_network.get_paired_peer(self.settings, "target-213"))

    def test_missing_anchor_admin_does_not_send_code(self):
        peer, received = self.serve(attacker=True)
        self.discover(peer)
        self.attempt(peer)
        self.assert_secret_not_sent(received)
        self.assertEqual(self.handler._send_json.call_args.args[0], 400)

    def test_pairing_primitive_also_refuses_missing_anchor(self):
        peer, received = self.serve(attacker=True)
        self.attempt(peer, direct=True)
        self.assert_secret_not_sent(received)

    def test_nonhex_confirmation_cannot_disable_tls_verification(self):
        peer, received = self.serve(attacker=True)
        self.discover(peer)
        self.attempt(peer, "ZZ:ZZ:ZZ")
        self.assert_secret_not_sent(received)

    def test_pair_by_address_nonhex_confirmation_does_not_leak(self):
        peer, received = self.serve(attacker=True)
        self.attempt(peer, "ZZ:ZZ:ZZ", by_address=True)
        self.assert_secret_not_sent(received)

    def test_discovered_http_downgrade_does_not_send_code(self):
        peer, received = self.serve(tls=False, attacker=True)
        self.discover(peer)
        self.attempt(peer)
        self.assert_secret_not_sent(received)

    def test_confirmation_does_not_make_discovered_http_safe(self):
        peer, received = self.serve(tls=False, attacker=True)
        self.discover(peer)
        self.attempt(peer, self.remote["fingerprint"])
        self.assert_secret_not_sent(received)

    def test_wrong_tls_certificate_receives_no_request(self):
        peer, received = self.serve(attacker=True)
        self.discover(peer)
        self.attempt(peer, self.remote["fingerprint"])
        self.assert_secret_not_sent(received)

    def test_confirmed_identity_certificate_pairs_successfully(self):
        peer, received = self.serve()
        self.discover(peer)
        fingerprint = ":".join(self.remote["fingerprint"][i:i+2] for i in range(0, 64, 2)).upper()
        self.attempt(peer, fingerprint)
        self.assertEqual(self.handler._send_json.call_args.args[0], 200)
        self.assertEqual(received[0]["pairing_code"], CODE)
        stored = local_network.get_paired_peer(self.settings, "target-213")
        self.assertEqual(stored["certificate_fingerprint"], self.remote["fingerprint"])
        self.assertEqual(Path(stored["certificate_path"]).read_text(), self.remote["public_certificate"])

    def test_pair_by_address_confirmed_identity_pairs_successfully(self):
        peer, received = self.serve()
        self.attempt(peer, self.remote["fingerprint"], by_address=True)
        self.assertEqual(self.handler._send_json.call_args.args[0], 200)
        self.assertEqual(received[0]["pairing_code"], CODE)
        self.assertEqual(local_network.get_paired_peer(self.settings, "target-213")["certificate_fingerprint"],
                         self.remote["fingerprint"])

    def test_target_pairing_card_authenticates_production_https_leaf(self):
        peer, received = self.serve(leaf=True)
        self.discover(peer)
        target_handler = handlers_network.HandlersNetworkMixin()
        target_handler.settings = self.remote_settings
        card = target_handler._pairing_card()
        self.assertEqual(card["certificate_fingerprint"], self.remote["fingerprint"])
        self.attempt(peer, card["certificate_fingerprint"])
        self.assertEqual(self.handler._send_json.call_args.args[0], 200,
                         "fingerprint from target's pairing card must support its real HTTPS listener")
        self.assertEqual(received[0]["pairing_code"], CODE)

    def install_pin(self, peer):
        path, fingerprint = peer_connectivity._save_local_peer_certificate(
            self.settings, "target-213", self.remote["public_certificate"])
        stored = local_network.save_paired_peer(self.settings, {
            **peer, "certificate_fingerprint": fingerprint, "certificate_path": str(path)})
        return path, stored

    def test_rejected_declared_fingerprint_preserves_live_pin_file(self):
        peer, received = self.serve(response_updates={"certificate_fingerprint": "00" * 32})
        path, before = self.install_pin(peer)
        original = path.read_bytes()
        self.attempt(peer, direct=True)
        self.assertEqual(len(received), 1, "authenticated test endpoint must receive request")
        self.assertTrue(path.exists(), "rejected response deleted the existing trust anchor")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(local_network.get_paired_peer(self.settings, "target-213"), before)

    def test_rejected_offered_certificate_preserves_pin_and_record(self):
        peer, received = self.serve(response_updates={"certificate_pem": self.attacker["public_certificate"]})
        path, before = self.install_pin(peer)
        original = path.read_bytes()
        self.attempt(peer, direct=True)
        self.assertEqual(len(received), 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(local_network.get_paired_peer(self.settings, "target-213"), before)

    def test_wrong_identity_response_preserves_pin_and_record(self):
        peer, received = self.serve(response_updates={"drone_id": "other-213"})
        path, before = self.install_pin(peer)
        original = path.read_bytes()
        self.attempt(peer, direct=True)
        self.assertEqual(len(received), 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(local_network.get_paired_peer(self.settings, "target-213"), before)


if __name__ == "__main__":
    unittest.main()
