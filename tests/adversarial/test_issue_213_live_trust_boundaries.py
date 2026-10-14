"""#213: trust requires possession of a key on the secret-bearing connection.

A public identity certificate appended to a hostile TLS chain is not proof of
possession. A preflight handshake is not proof for a later request. Conversely,
a saved pin is an independent anchor and must allow legitimate re-pairing without
accepting conflicting operator input. All endpoints and state are local fixtures.
"""
import socket
import ssl
import threading
import unittest
from pathlib import Path

import test_issue_213_pairing_trust_uat as fixtures
from app.transfer import local_network


class LiveTrustBoundaries(unittest.TestCase):
    # Reuse only fixture helpers, not the earlier tester's test methods.
    setUpClass = classmethod(fixtures.PairingTrustUAT.setUpClass.__func__)
    setUp = fixtures.PairingTrustUAT.setUp
    serve = fixtures.PairingTrustUAT.serve
    discover = fixtures.PairingTrustUAT.discover
    attempt = fixtures.PairingTrustUAT.attempt
    install_pin = fixtures.PairingTrustUAT.install_pin
    assert_secret_not_sent = fixtures.PairingTrustUAT.assert_secret_not_sent

    def test_saved_pin_allows_repair_without_announcement_confirmation(self):
        peer, received = self.serve()
        path, _ = self.install_pin(peer)
        discovered = self.discover({**peer, "certificate_fingerprint": self.attacker["fingerprint"]})
        self.assertEqual(discovered["certificate_fingerprint"], self.remote["fingerprint"])
        self.attempt(peer)
        self.assertEqual(self.handler._send_json.call_args.args[0], 200)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["pairing_code"], fixtures.CODE)
        self.assertEqual(path.read_text(), self.remote["public_certificate"])

    def test_conflicting_confirmation_cannot_replace_existing_pin(self):
        peer, received = self.serve(attacker=True)
        path, before = self.install_pin(peer)
        original = path.read_bytes()
        self.discover(peer)
        self.attempt(peer, self.attacker["fingerprint"])
        self.assertEqual(received, [], "conflicting confirmation must fail before sending the code")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(local_network.get_paired_peer(self.settings, "target-213"), before)

    def scripted_tls(self, contexts, *, record_from=0):
        """Serve bounded handshakes with a controlled certificate per connection."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.settimeout(3)
        port = listener.getsockname()[1]
        received, handshakes, errors = [], [], []
        done = threading.Event()

        def run():
            try:
                for index, context in enumerate(contexts):
                    try:
                        raw, _ = listener.accept()
                    except socket.timeout:
                        break  # a client may reject before exhausting the script
                    raw.settimeout(3)
                    try:
                        with context.wrap_socket(raw, server_side=True) as tls:
                            handshakes.append(True)
                            data = tls.recv(65536)
                            if data:
                                if index >= record_from:
                                    received.append(data)
                                # Even if the client sends a secret, finish its
                                # request promptly so the assertion can expose it.
                                tls.sendall(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')
                    except (OSError, ssl.SSLError):
                        raw.close()  # client rejection is expected
            except Exception as error:
                errors.append(error)
            finally:
                listener.close()
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def close():
            listener.close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "TLS fixture did not stop")

        self.addCleanup(close)
        peer = {"drone_id": "target-213", "reachable_url": f"https://127.0.0.1:{port}",
                "certificate_fingerprint": self.attacker["fingerprint"]}
        return peer, received, handshakes, errors, done

    def tls_context(self, settings, certificate=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certificate or settings.drone_cert_file), str(settings.drone_key_file))
        return context

    def test_tls_probe_cannot_authorize_a_different_request_certificate(self):
        peer, received, handshakes, errors, done = self.scripted_tls([
            self.tls_context(self.remote_settings), self.tls_context(self.attacker_settings),
        ], record_from=1)
        self.attempt(peer, self.remote["fingerprint"], direct=True)
        self.assertTrue(done.wait(5), "both probe and request handshakes must finish")
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(handshakes), 1, "TLS fixture must actually be reached")
        # A client that eliminates preflight and uses only one verified socket
        # is also safe: only bytes received by the rogue context are forbidden.
        self.assert_secret_not_sent(received)

    def test_appending_public_identity_to_rogue_chain_does_not_prove_possession(self):
        chain = Path(self.settings.userdata_root) / "rogue-chain.pem"
        chain.write_text(self.attacker["public_certificate"] + self.remote["public_certificate"])
        context = self.tls_context(self.attacker_settings, chain)
        # Current path probes the leaf, retrieves the chain, then performs a
        # verified live request. The third handshake must reject the rogue leaf.
        peer, received, handshakes, errors, done = self.scripted_tls([context] * 3)
        self.attempt(peer, self.remote["fingerprint"], direct=True)
        self.assertTrue(done.wait(5), "chain verification fixture did not finish")
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(handshakes), 1, "TLS fixture must actually be reached")
        self.assert_secret_not_sent(received)


if __name__ == "__main__":
    unittest.main()
