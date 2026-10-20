"""Out-of-scope evidence for #279: a decodable non-certificate PEM is persisted as a trust pin.

Issue #279 is about a paired answer with no certificate. This is a different boundary: the
certificate marker and PEM envelope are present and the base64 body decodes, but the bytes
are not an X.509 certificate. ``_certificate_pem_fingerprint`` only base64-decodes the body,
so ``_save_local_peer_certificate`` writes the file and its fingerprint becomes the pin. The
expected behaviour below is that the save step rejects it before writing anything.

Kept in a separate suite so it does not block #279 delivery.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.transfer import peer_connectivity


PEER_ID = "bb:bb:bb:bb:02:79"
# "AAAA" decodes to three zero bytes: a valid base64 body that is not DER-encoded X.509.
NON_CERTIFICATE_PEM = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"


class DecodableNonCertificatePinTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self._tmp.name).resolve())
        self.cache_path = (
            self.settings.userdata_root / "system" / "drone-app" / "local-peer-certs" / f"{PEER_ID}.crt"
        )

    def test_save_step_rejects_a_non_certificate_body_before_writing_a_pin(self) -> None:
        with self.assertRaises(ValueError):
            peer_connectivity._save_local_peer_certificate(self.settings, PEER_ID, NON_CERTIFICATE_PEM)
        self.assertFalse(self.cache_path.exists(), "a non-certificate body must not become a trust pin")


if __name__ == "__main__":
    unittest.main()
