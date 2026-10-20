"""Unauthenticated LAN announcements cannot redirect pairing (issue #213)."""

import hashlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity
from app.web import handlers_network

FINGERPRINT = "ab" * 32


def _announcement(drone_id: str, fingerprint: str = "bb" * 32, **extra) -> dict:
    return {
        "service": local_network.DISCOVERY_SERVICE,
        "drone_id": drone_id,
        "name": "Cabinet",
        "scheme": "https",
        "api_port": 443,
        "reachable_url": "https://attacker.local",
        "certificate_fingerprint": fingerprint,
        **extra,
    }


class AnnouncementCannotRepointPairedPeerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = {"USERDATA_ROOT": str(Path(self._tmp.name) / "userdata"), "DRONE_DEVICE_ID": "local-a"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)

    def _pair_victim(self) -> None:
        local_network.save_paired_peer(
            self.settings,
            {
                "drone_id": "victim",
                "reachable_url": "https://192.168.1.20",
                "advertised_reachable_url": "https://victim.local",
                "certificate_fingerprint": "aa" * 32,
                "tailnet_ip": "100.64.0.9",
            },
        )

    def test_spoofed_announcement_keeps_paired_address_and_fingerprint(self) -> None:
        self._pair_victim()
        discovered = local_network.record_discovered_peer(
            self.settings, _announcement("victim", tailnet_ip="100.64.0.66"), "10.66.0.9"
        )
        stored = local_network.get_paired_peer(self.settings, "victim")
        self.assertEqual(stored["reachable_url"], "https://192.168.1.20")
        self.assertEqual(stored["advertised_reachable_url"], "https://victim.local")
        self.assertEqual(stored["certificate_fingerprint"], "aa" * 32)
        self.assertEqual(stored["tailnet_ip"], "100.64.0.9")
        self.assertEqual(peer_connectivity._peer_address(stored), "https://192.168.1.20")
        # The discovered row the admin pair action reads mirrors the pinned record.
        self.assertEqual(discovered["reachable_url"], "https://192.168.1.20")
        self.assertEqual(discovered["certificate_fingerprint"], "aa" * 32)
        self.assertEqual(discovered["announced_reachable_url"], "https://10.66.0.9")
        self.assertEqual(discovered["announced_certificate_fingerprint"], "bb" * 32)

    def test_announcement_still_refreshes_last_seen(self) -> None:
        self._pair_victim()
        before = local_network.get_paired_peer(self.settings, "victim")["last_seen"]
        with mock.patch.object(local_network, "_now_iso", return_value="2999-01-01T00:00:00+00:00"):
            local_network.record_discovered_peer(self.settings, _announcement("victim"), "10.66.0.9")
        self.assertNotEqual(local_network.get_paired_peer(self.settings, "victim")["last_seen"], before)

    def test_unpaired_peer_announcement_still_sets_address(self) -> None:
        discovered = local_network.record_discovered_peer(self.settings, _announcement("newcomer"), "192.168.1.30")
        self.assertEqual(discovered["reachable_url"], "https://192.168.1.30")


class PairCodeRequiresCertificateAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler._send_json = mock.Mock()
        self.peer = {"drone_id": "peer-a", "reachable_url": "https://10.66.0.9", "certificate_fingerprint": "bb" * 32}

    def test_https_pairing_code_is_not_sent_without_operator_confirmation(self) -> None:
        with mock.patch.object(handlers_network, "_local_pair_peer") as pair, \
                mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=None):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "12345678"))
        pair.assert_not_called()
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 400)
        self.assertTrue(payload["certificate_fingerprint_required"])

    def test_announced_fingerprint_is_not_a_substitute_for_confirmation(self) -> None:
        with mock.patch.object(handlers_network, "_local_pair_peer") as pair, \
                mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=None):
            self.handler._pair_peer_or_reply({**self.peer, "certificate_fingerprint": FINGERPRINT}, "12345678", "")
        pair.assert_not_called()

    def test_confirmed_fingerprint_is_forwarded(self) -> None:
        with mock.patch.object(handlers_network, "_local_pair_peer", return_value={"drone_id": "peer-a"}) as pair, \
                mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=None):
            self.handler._pair_peer_or_reply(self.peer, "12345678", FINGERPRINT)
        self.assertEqual(pair.call_args.kwargs["confirmed_fingerprint"], FINGERPRINT)

    def test_pinned_peer_does_not_need_confirmation(self) -> None:
        pinned = {"drone_id": "peer-a", "certificate_fingerprint": FINGERPRINT}
        with mock.patch.object(handlers_network, "_local_pair_peer", return_value={"drone_id": "peer-a"}) as pair, \
                mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=pinned):
            self.handler._pair_peer_or_reply(self.peer, "12345678")
        pair.assert_called_once()

    def test_cleartext_address_still_needs_a_confirmed_fingerprint(self) -> None:
        peer = {**self.peer, "reachable_url": "http://127.0.0.1:8080"}
        with mock.patch.object(handlers_network, "_local_pair_peer") as pair, \
                mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=None):
            self.handler._pair_peer_or_reply(peer, "12345678")
        pair.assert_not_called()
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 400)
        self.assertTrue(payload["certificate_fingerprint_required"])

    def test_cleartext_address_with_confirmed_fingerprint_is_forwarded(self) -> None:
        peer = {**self.peer, "reachable_url": "http://127.0.0.1:8080"}
        with mock.patch.object(handlers_network, "_local_pair_peer", return_value={"drone_id": "peer-a"}) as pair, \
                mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=None):
            self.handler._pair_peer_or_reply(peer, "12345678", FINGERPRINT)
        self.assertEqual(pair.call_args.kwargs["confirmed_fingerprint"], FINGERPRINT)


class LocalPairPeerPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = {"USERDATA_ROOT": str(Path(self._tmp.name) / "userdata"), "DRONE_DEVICE_ID": "local-a"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)

    def test_confirmed_fingerprint_must_agree_with_pinned_record(self) -> None:
        local_network.save_paired_peer(
            self.settings, {"drone_id": "peer-a", "reachable_url": "https://10.0.0.5", "certificate_fingerprint": FINGERPRINT}
        )
        with mock.patch.object(peer_connectivity, "urlopen") as opened:
            with self.assertRaisesRegex(ValueError, "does not match"):
                peer_connectivity._local_pair_peer(
                    self.settings,
                    {"drone_id": "peer-a", "reachable_url": "https://10.0.0.5"},
                    "12345678",
                    confirmed_fingerprint="cd" * 32,
                )
        opened.assert_not_called()

    def test_rejected_certificate_does_not_overwrite_pinned_certificate_file(self) -> None:
        local_network.save_paired_peer(
            self.settings, {"drone_id": "peer-a", "reachable_url": "https://10.0.0.5", "certificate_fingerprint": FINGERPRINT}
        )
        pin_file = peer_connectivity._local_peer_cert_cache_path(self.settings, "peer-a")
        pin_file.parent.mkdir(parents=True, exist_ok=True)
        pin_file.write_text("original pin", encoding="utf-8")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"status": "paired", "drone_id": "peer-a", "certificate_pem": "-----BEGIN CERTIFICATE-----rogue"}
        ).encode("utf-8")
        fake_manager = mock.MagicMock()
        fake_manager.return_value.ensure_certificate.return_value = {
            "public_certificate": "-----BEGIN CERTIFICATE-----own",
            "fingerprint": "cc" * 32,
        }
        with mock.patch.object(peer_connectivity, "urlopen", return_value=response), \
                mock.patch.object(peer_connectivity, "_pairing_tls_context"), \
                mock.patch.object(peer_connectivity, "DroneCertificateManager", fake_manager), \
                mock.patch.object(peer_connectivity, "_certificate_pem_fingerprint", return_value="ee" * 32):
            with self.assertRaises(RuntimeError):
                peer_connectivity._local_pair_peer(
                    self.settings, {"drone_id": "peer-a", "reachable_url": "https://10.0.0.5"}, "12345678"
                )
        self.assertEqual(pin_file.read_text(encoding="utf-8"), "original pin")


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class PinnedContextTests(unittest.TestCase):
    """A real TLS handshake: bytes only reach the holder of the expected key."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _certificate(self, name: str) -> tuple[Path, Path, str]:
        cert, key = self.tmp / f"{name}.crt", self.tmp / f"{name}.key"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
             "-subj", f"/CN={name}", "-days", "2"],
            check=True, capture_output=True,
        )
        der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="utf-8"))
        return cert, key, hashlib.sha256(der).hexdigest()

    def _serve(self, cert: Path, key: Path):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        received: list[bytes] = []

        def run() -> None:
            try:
                raw, _ = listener.accept()
                raw.settimeout(2)
                with context.wrap_socket(raw, server_side=True) as conn:
                    data = conn.recv(65536)
                    received.append(data)
                    body = b'{"status": "paired"}'
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
            except (OSError, ssl.SSLError):
                pass
            finally:
                listener.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return listener.getsockname()[1], received

    def _post(self, port: int, expected_fingerprint: str):
        request = Request(f"https://127.0.0.1:{port}/v1/api/peer/pair", data=b'{"pairing_code": "12345678"}', method="POST")
        return urlopen(request, timeout=5, context=peer_connectivity._pinned_pairing_context(expected_fingerprint))

    def test_wrong_certificate_never_receives_the_request(self) -> None:
        _, _, expected = self._certificate("real")
        cert, key, _ = self._certificate("attacker")
        port, received = self._serve(cert, key)
        with self.assertRaises((URLError, ssl.SSLError)):
            self._post(port, expected)
        self.assertEqual(received, [])

    def test_expected_certificate_is_accepted_with_colon_formatting(self) -> None:
        cert, key, fingerprint = self._certificate("real")
        port, received = self._serve(cert, key)
        formatted = ":".join(fingerprint[i:i + 2] for i in range(0, len(fingerprint), 2)).upper()
        with self._post(port, formatted) as response:
            self.assertEqual(json.loads(response.read())["status"], "paired")
        self.assertIn(b"POST /v1/api/peer/pair", received[0])


if __name__ == "__main__":
    unittest.main()
