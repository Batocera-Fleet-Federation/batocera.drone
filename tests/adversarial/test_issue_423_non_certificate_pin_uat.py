"""Issue #423 UAT: a PEM envelope around non-certificate bytes must not become a trust pin.

A pairing response or gossiped roster card carries a PEM block. Base64-decoding the
body is not enough to call it a certificate: the bytes must parse as X.509 before the
save step writes a file and the SHA-256 of those bytes becomes the peer's pin.
"""

from __future__ import annotations

import hashlib
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer.drone_network import _certificate_pem_fingerprint
from app.transfer.peer_connectivity import _local_peer_cert_cache_path, _save_local_peer_certificate
from app.transfer.drone_tls import DroneCertificateManager


NON_CERTIFICATE_PEM = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"


class NonCertificatePinUatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        env = {
            "USERDATA_ROOT": str(self.root / "userdata"),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "drone-issue-423",
            "LOG_DIR": str(self.root / "logs"),
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_decodable_non_certificate_is_rejected_before_any_write(self) -> None:
        with self.assertRaises(ValueError):
            _save_local_peer_certificate(self.settings, "peer-a", NON_CERTIFICATE_PEM)
        self.assertFalse(_local_peer_cert_cache_path(self.settings, "peer-a").exists())

    def test_fingerprint_helper_rejects_decodable_non_certificate(self) -> None:
        with self.assertRaises(ValueError):
            _certificate_pem_fingerprint(NON_CERTIFICATE_PEM)

    def test_pem_wrapping_a_non_x509_der_blob_is_rejected(self) -> None:
        # Valid base64 of arbitrary bytes inside a certificate envelope.
        blob = ssl.DER_cert_to_PEM_cert(b"\x30\x03\x02\x01\x01")
        with self.assertRaises(ValueError):
            _certificate_pem_fingerprint(blob)

    def test_genuine_certificate_still_pins_and_persists(self) -> None:
        pem = str(DroneCertificateManager(self.settings).ensure_certificate().get("public_certificate") or "")
        self.assertIn("BEGIN CERTIFICATE", pem)
        expected = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()

        cert_path, fingerprint = _save_local_peer_certificate(self.settings, "peer-b", pem)

        self.assertEqual(fingerprint, expected)
        self.assertEqual(cert_path.read_text(encoding="utf-8"), pem)

    def test_missing_envelope_still_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _save_local_peer_certificate(self.settings, "peer-c", "not a certificate")


if __name__ == "__main__":
    unittest.main()
