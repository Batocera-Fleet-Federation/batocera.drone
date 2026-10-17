"""#337: only possession of a paired identity authorizes route/port refresh.

Discovery may claim an id or copy its public fingerprint. Neither claim grants
route authority. Exercise the complete discovery -> authenticated fetch -> real
SQLite save path, with independent TLS fixtures and no mocked trust decisions.
"""
from __future__ import annotations

import hashlib
import json
import socket
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity
from app.web import handlers_network


FIXTURES = Path(__file__).parent / "fixtures" / "issue_337"
PEER = "paired-337"
ROUTE_FIELDS = (
    "reachable_url", "advertised_reachable_url", "public_reachable_url",
    "tailnet_ip", "tailnet_id", "dns_name", "scheme", "api_port",
    "peer_mtls_port", "certificate_fingerprint", "certificate_path", "paired_over_tls",
)


def fingerprint(name):
    der = ssl.PEM_cert_to_DER_cert((FIXTURES / (name + ".crt")).read_text())
    return hashlib.sha256(der).hexdigest()


class TailnetRefreshUAT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="issue-337-uat-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        env = {"USERDATA_ROOT": str(root), "DRONE_DEVICE_ID": "local-337",
               "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
               "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios"),
               "SAVES_ROOT": str(root / "saves")}
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)
        self.before = local_network.save_paired_peer(self.settings, {
            "drone_id": PEER, "scheme": "https", "paired_over_tls": True,
            "reachable_url": "https://192.0.2.20:7443",
            "advertised_reachable_url": "https://old-peer.invalid:7443",
            "public_reachable_url": "https://192.0.2.21:7443",
            "tailnet_ip": "100.64.0.9", "tailnet_id": "old-tailnet-node",
            "dns_name": "old-peer.test.ts.net", "api_port": 7443,
            "peer_mtls_port": 8543, "certificate_fingerprint": fingerprint("victim"),
            "certificate_path": str(FIXTURES / "victim.crt"),
        })
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = self.settings
        # Pair repair is a subsequent operation, outside this metadata refresh.
        self.handler._repair_one_sided_pairing = mock.Mock(side_effect=lambda peer: peer)
        self.info = {"service": local_network.DISCOVERY_SERVICE, "drone_id": PEER,
                     "scheme": "https", "api_port": 9443, "peer_mtls_port": 9543,
                     "reachable_url": "https://new-peer.invalid:9443"}
        gate = mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True)
        gate.start()
        self.addCleanup(gate.stop)

    def serve(self, replies, *, identities=("victim",), tls=True):
        """Each HTTP request gets a (status, raw body, headers) reply.

        TLS identities advance per handshake, including handshake-only probes;
        this lets a server change certs between verification and the data fetch.
        """
        requests, handshakes = [], []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                index = len(requests)
                requests.append(self.path)
                reply = replies[min(index, len(replies) - 1)]
                if reply is None:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                status, raw, headers = reply
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        if tls:
            contexts = []
            for name in identities:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                cert_name = "leaf-chain" if name == "leaf" else name
                context.load_cert_chain(FIXTURES / (cert_name + ".crt"), FIXTURES / (name + ".key"))
                contexts.append(context)

            def select_identity(connection, _hostname, _context):
                index = len(handshakes)
                handshakes.append(index)
                connection.context = contexts[min(index, len(contexts) - 1)]

            contexts[0].set_servername_callback(select_identity)
            server.socket = contexts[0].wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "loopback fixture did not stop")

        self.addCleanup(cleanup)
        scheme = "https" if tls else "http"
        return f"{scheme}://127.0.0.1:{server.server_port}", requests, handshakes

    def reply(self, payload=None, status=200, headers=None):
        return status, json.dumps(self.info if payload is None else payload).encode(), headers or {}

    def sync(self, address, **kwargs):
        return self.handler._sync_tailnet_device(
            {"tailnet_ip": address, "tailnet_id": "new-node", "dns_name": "new.test.ts.net"}, **kwargs)

    def assert_preserved(self):
        stored = local_network.get_paired_peer(self.settings, PEER)
        self.assertEqual({k: stored.get(k) for k in ROUTE_FIELDS},
                         {k: self.before.get(k) for k in ROUTE_FIELDS})
        self.handler._repair_one_sided_pairing.assert_not_called()
        discovered = next((p for p in local_network.discovered_peers(self.settings, include_stale=True)
                           if p["drone_id"] == PEER), None)
        if discovered:
            for field in ("reachable_url", "advertised_reachable_url", "tailnet_ip", "api_port", "peer_mtls_port", "certificate_fingerprint"):
                # Discovery may omit an absent/empty legacy pin. It must never
                # populate that missing authority from the bootstrap body.
                if field == "certificate_fingerprint" and not self.before.get(field):
                    self.assertFalse(discovered.get(field), field)
                else:
                    self.assertEqual(discovered.get(field), self.before.get(field), field)

    def test_full_probe_rejects_foreign_key_with_missing_empty_or_copied_public_fingerprint(self):
        for claimed in (None, "", fingerprint("victim")):
            with self.subTest(claimed=claimed):
                spoof = {**self.info, "scheme": "http", "api_port": 9999, "peer_mtls_port": 9999}
                if claimed is not None:
                    spoof["certificate_fingerprint"] = claimed
                address, requests, _ = self.serve([self.reply(spoof)], identities=("other",))
                result = self.sync(address)
                self.assertIn("tailnet_identity_error", result)
                self.assertEqual(requests, ["/v1/api/peer/info"])
                self.assert_preserved()

    def test_certificate_switch_after_verification_probe_cannot_refresh(self):
        spoof = {**self.info, "certificate_fingerprint": fingerprint("victim")}
        address, requests, handshakes = self.serve(
            [self.reply(spoof)], identities=("victim", "victim", "other"))
        result = self.sync(address)
        self.assertIn("tailnet_identity_error", result)
        self.assertEqual(len(handshakes), 3, "must reach the actual data connection")
        self.assertEqual(requests, ["/v1/api/peer/info"], "foreign TLS key received an authenticated GET")
        self.assert_preserved()

    def test_authenticated_body_alone_repairs_routes_and_legacy_port(self):
        # A pre-listener-split record needs the new mTLS port; bootstrap lies
        # about every routing field and cannot override the verified response.
        local_network.save_paired_peer(self.settings, {**self.before, "peer_mtls_port": None})
        bootstrap = {**self.info, "api_port": 6666, "peer_mtls_port": 6667,
                     "scheme": "http", "reachable_url": "http://wrong.invalid:6666",
                     "certificate_fingerprint": fingerprint("other")}
        address, requests, _ = self.serve([self.reply(bootstrap), self.reply()])
        result = self.sync(address)
        self.assertNotIn("tailnet_identity_error", result)
        stored = local_network.get_paired_peer(self.settings, PEER)
        for field in ("api_port", "peer_mtls_port", "scheme"):
            self.assertEqual(stored[field], self.info[field])
        self.assertEqual(stored["advertised_reachable_url"], self.info["reachable_url"])
        self.assertEqual(stored["reachable_url"], self.before["reachable_url"])
        self.assertEqual(stored["certificate_fingerprint"], self.before["certificate_fingerprint"])
        self.assertEqual(stored["tailnet_ip"], address)
        self.assertEqual(requests, ["/v1/api/peer/info"] * 2)
        self.handler._repair_one_sided_pairing.assert_called_once()

    def test_independently_signed_https_leaf_can_repair_paired_route(self):
        address, requests, _ = self.serve([self.reply()], identities=("leaf",))
        result = self.sync(address)
        self.assertNotIn("tailnet_identity_error", result)
        stored = local_network.get_paired_peer(self.settings, PEER)
        self.assertEqual(stored["tailnet_ip"], address)
        self.assertEqual(stored["peer_mtls_port"], 9543)
        self.assertEqual(stored["certificate_fingerprint"], fingerprint("victim"))
        self.assertEqual(len(requests), 2)

    def test_verified_tls_wrong_identity_or_service_cannot_refresh(self):
        for changed in ({"drone_id": "another-peer"}, {"drone_id": ""},
                        {"service": "other-service"}, {"service": None}):
            with self.subTest(changed=changed):
                address, requests, _ = self.serve([self.reply(), self.reply({**self.info, **changed})])
                result = self.sync(address)
                self.assertIn("tailnet_identity_error", result)
                self.assertEqual(len(requests), 2)
                self.assert_preserved()

    def test_malformed_or_interrupted_verified_response_preserves_routes(self):
        for raw in (b"{", b"[]", b"null", b"", b"\xff", None):
            with self.subTest(raw=raw):
                response = None if raw is None else (200, raw, {})
                address, requests, _ = self.serve([self.reply(), response])
                result = self.sync(address)
                self.assertIn("tailnet_identity_error", result)
                self.assertEqual(len(requests), 2)
                self.assert_preserved()

    def test_redirect_and_http_error_never_supply_refresh_metadata(self):
        target, followed, _ = self.serve([self.reply()], tls=False)
        for status in (301, 302, 303, 307, 308, 401, 404, 500):
            with self.subTest(status=status):
                address, requests, _ = self.serve([
                    self.reply(), self.reply(status=status, headers={"Location": target})])
                result = self.sync(address)
                self.assertIn("tailnet_identity_error", result)
                self.assertEqual(len(requests), 2)
                self.assertEqual(followed, [], "rejected response caused an HTTP redirect")
                self.assert_preserved()

    def test_bootstrap_pin_cannot_replace_missing_or_invalid_persisted_pin(self):
        for pin in (None, "", "ab" * 31, "not-a-sha256-pin"):
            with self.subTest(pin=pin):
                self.before = local_network.save_paired_peer(
                    self.settings, {**self.before, "certificate_fingerprint": pin})
                address, requests, _ = self.serve([
                    self.reply({**self.info, "certificate_fingerprint": fingerprint("victim")})])
                result = self.sync(address)
                self.assertIn("tailnet_identity_error", result)
                self.assertEqual(requests, ["/v1/api/peer/info"])
                self.assert_preserved()

    def test_invalid_pin_or_plaintext_refresh_fails_before_network_dial(self):
        for address, pin in (("http://127.0.0.1:9", fingerprint("victim")),
                             ("https://127.0.0.1:9", ""),
                             ("https://127.0.0.1:9", "not-a-sha256-pin"),
                             ("https://127.0.0.1:9", "ab" * 31)):
            with self.subTest(address=address, pin=pin), \
                    mock.patch.object(peer_connectivity.socket, "create_connection") as dial:
                with self.assertRaises(ssl.SSLError):
                    peer_connectivity._fetch_peer_info(address, expected_fingerprint=pin)
                dial.assert_not_called()


if __name__ == "__main__":
    unittest.main()
