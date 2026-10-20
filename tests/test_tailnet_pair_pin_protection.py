"""A tailnet-authorised (code-free) pair request must not replace a pinned peer certificate (issue #430)."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity
from app.transfer.drone_network import _certificate_pem_fingerprint
from app.web import handlers_peer

TAILNET_IP = "100.64.0.9"


def _self_signed(directory: Path, name: str) -> str:
    key, crt = directory / f"{name}.key", directory / f"{name}.crt"
    subprocess.run(
        ["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(crt),
         "-days", "2", "-subj", f"/CN={name}"],
        check=True, capture_output=True,
    )
    return crt.read_text(encoding="utf-8")


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class TailnetPairPinProtectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        env = {"USERDATA_ROOT": str(root / "userdata"), "DRONE_DEVICE_ID": "local-b"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)
        certs = root / "certs"
        certs.mkdir()
        self.genuine_pem = _self_signed(certs, "genuine")
        self.attacker_pem = _self_signed(certs, "attacker")
        self.genuine_fp = _certificate_pem_fingerprint(self.genuine_pem)
        cert_path, _ = peer_connectivity._save_local_peer_certificate(self.settings, "victim", self.genuine_pem)
        self.cert_path = cert_path
        local_network.save_paired_peer(
            self.settings,
            {"drone_id": "victim", "scheme": "https", "reachable_url": "https://192.168.1.20",
             "certificate_fingerprint": self.genuine_fp, "certificate_path": str(cert_path), "paired_over_tls": True},
        )

    def _pair(self, payload: dict, *, code_valid: bool = False, client_ip: str = TAILNET_IP):
        handler = handlers_peer.HandlersPeerMixin()
        handler.settings = self.settings
        handler.client_address = (client_ip, 12345)
        handler.server = mock.MagicMock()
        handler._send_json = mock.MagicMock()
        certificate = {"public_certificate": "-----BEGIN CERTIFICATE-----own", "fingerprint": "cc:dd"}
        manager = mock.MagicMock()
        manager.return_value.ensure_certificate.return_value = certificate
        with mock.patch.object(handlers_peer, "tailnet_peer_ips", return_value={TAILNET_IP}), \
                mock.patch.object(handlers_peer._local_network, "validate_pairing_code", return_value=code_valid), \
                mock.patch.object(handlers_peer, "DroneCertificateManager", manager), \
                mock.patch.object(handlers_peer, "load_peer_cert_everywhere"), \
                mock.patch("app.transfer.swarm_membership.record_approved_member"):
            try:
                handler._pair_error = None
                handler._handle_peer_pair(payload)
            except ValueError as error:
                handler._pair_error = error
        return handler

    def _assert_pin_untouched(self) -> None:
        self.assertEqual(self.cert_path.read_text(encoding="utf-8"), self.genuine_pem)
        stored = local_network.get_paired_peer(self.settings, "victim")
        self.assertEqual(stored["certificate_fingerprint"], self.genuine_fp)
        self.assertEqual(stored["reachable_url"], "https://192.168.1.20")

    def test_tailnet_request_cannot_replace_pinned_certificate(self) -> None:
        handler = self._pair({"tailnet_auto_pair": True, "drone_id": "victim", "scheme": "https",
                              "certificate_pem": self.attacker_pem})
        self.assertEqual(handler._send_json.call_args[0][0], 409)
        self._assert_pin_untouched()

    def test_tailnet_request_cannot_replace_pin_with_mismatched_claimed_fingerprint(self) -> None:
        handler = self._pair({"tailnet_auto_pair": True, "drone_id": "victim", "scheme": "https",
                              "certificate_pem": self.attacker_pem, "certificate_fingerprint": "00" * 32})
        self.assertEqual(handler._send_json.call_args[0][0], 409)
        self._assert_pin_untouched()

    def test_tailnet_request_with_unreadable_certificate_leaves_pin(self) -> None:
        handler = self._pair({"tailnet_auto_pair": True, "drone_id": "victim", "scheme": "https",
                              "certificate_pem": "-----BEGIN CERTIFICATE-----junk"})
        self.assertEqual(handler._send_json.call_args[0][0], 409)
        self._assert_pin_untouched()

    def test_tailnet_request_refused_when_only_cache_file_is_pinned(self) -> None:
        local_network.forget_peer(self.settings, "victim", publish_membership=False)
        self.cert_path.write_text(self.genuine_pem, encoding="utf-8")
        handler = self._pair({"tailnet_auto_pair": True, "drone_id": "victim", "scheme": "https",
                              "certificate_pem": self.attacker_pem})
        self.assertEqual(handler._send_json.call_args[0][0], 409)
        self.assertEqual(self.cert_path.read_text(encoding="utf-8"), self.genuine_pem)

    def test_tailnet_request_with_same_certificate_is_accepted(self) -> None:
        handler = self._pair({"tailnet_auto_pair": True, "drone_id": "victim", "scheme": "https",
                              "certificate_pem": self.genuine_pem})
        self.assertEqual(handler._send_json.call_args[0][0], 200)
        stored = local_network.get_paired_peer(self.settings, "victim")
        self.assertEqual(stored["certificate_fingerprint"], self.genuine_fp)

    def test_tailnet_request_for_unknown_peer_still_pairs(self) -> None:
        handler = self._pair({"tailnet_auto_pair": True, "drone_id": "newcomer", "scheme": "https",
                              "certificate_pem": self.attacker_pem})
        self.assertEqual(handler._send_json.call_args[0][0], 200)
        self.assertEqual(
            local_network.get_paired_peer(self.settings, "newcomer")["certificate_fingerprint"],
            _certificate_pem_fingerprint(self.attacker_pem),
        )

    def test_pairing_code_can_still_change_pinned_certificate(self) -> None:
        handler = self._pair({"pairing_code": "123456", "drone_id": "victim", "scheme": "https",
                              "certificate_pem": self.attacker_pem}, code_valid=True, client_ip="192.168.1.30")
        self.assertEqual(handler._send_json.call_args[0][0], 200)
        self.assertEqual(self.cert_path.read_text(encoding="utf-8"), self.attacker_pem)

    def test_rejected_fingerprint_mismatch_restores_existing_pin(self) -> None:
        handler = self._pair({"pairing_code": "123456", "drone_id": "victim", "scheme": "https",
                              "certificate_pem": self.attacker_pem, "certificate_fingerprint": "00" * 32},
                             code_valid=True, client_ip="192.168.1.30")
        self.assertIsNotNone(handler._pair_error)
        self.assertEqual(self.cert_path.read_text(encoding="utf-8"), self.genuine_pem)


if __name__ == "__main__":
    unittest.main()
