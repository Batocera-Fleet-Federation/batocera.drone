"""A pairing pin must be exactly one certificate's DER, never a certificate plus trailing bytes."""

from __future__ import annotations

import base64
import hashlib
import ssl
import unittest

from app.transfer.drone_network import _certificate_pem_fingerprint


# Self-signed fixture certificate (test-only key, discarded).
FIXTURE_CERT = """-----BEGIN CERTIFICATE-----
MIICpjCCAY4CCQC1MhuJRRIsYzANBgkqhkiG9w0BAQsFADAUMRIwEAYDVQQDDAlm
aXh0dXJlLTAwIBcNMjYxMDE2MDQ0NzU5WhgPMjEyNjA5MjIwNDQ3NTlaMBQxEjAQ
BgNVBAMMCWZpeHR1cmUtMDCCASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEB
AL1BZIeKXM47UziwKxiSWdWijU0gHQ3J+W1Ti0u/5wMP7yTKtSg50DN0zhXlI0xl
F6HYyHFZFEPOY605HmgFz1NLUlPA4pTlWodRuuV1C6RREJF1fcV6F92vNWLTZeNT
B1fbhsDj9YEOZ2xhtNZv6tbkh4VF6KySdbEmW2KFmTnQ4O/QgfitYy4AI7UYwDuu
ynB+CWO5k9NyzaeyzZ9SzpFDwVOBmSguUDj0oOTTto+UlhIYBwtGqKgiaFFdyp/b
FNLkRupSpcog9dUxKyXxPYehJnORsplhYlEgZngIt9KFZOE3A7VD3mLbobv0/JKL
NFieLrNy3euf0zIJpt7dVNECAwEAATANBgkqhkiG9w0BAQsFAAOCAQEAajBq6vvM
NEyZqRJ5unJ8EA2f70HXLXRoJgU6r8Wr6sEfnB05GOGrUlxXVyvi0GTbq5j0ioyX
Q0xWRBtsFJ6R0UbkqY4I5UajvAUMgIkgjjjjFMA6Uwo6MTvU3/9VmFC+VKLy6gWD
JbOsvVJyhkXzHS6FBNcXRpf3oLPTOcMIX1VvcJlHmnuO79VzJdhdoOJS32YthhYW
BCkvFFczJK0ADQJ31UJMJsIhIJiKd57hie/OVX8i6+FLW+h9k081uwapRamGh3cl
SnaCYIzIdfi19qUTInF2FlLBI8z2hE1JTUjwT0r7kmNXtmPKY5nC4A301IVSB9dp
i5EcDCn1e6KO7w==
-----END CERTIFICATE-----
"""


def _pem_from_der(der: bytes) -> str:
    body = base64.encodebytes(der).decode("ascii")
    return "-----BEGIN CERTIFICATE-----\n" + body + "-----END CERTIFICATE-----\n"


class RejectTrailingDerAfterCertificateTests(unittest.TestCase):
    def test_certificate_with_appended_der_bytes_is_not_fingerprinted_as_a_pin(self) -> None:
        der = ssl.PEM_cert_to_DER_cert(FIXTURE_CERT)
        # Control: the certificate on its own is a valid pin whose identity is its own DER.
        self.assertEqual(_certificate_pem_fingerprint(FIXTURE_CERT), hashlib.sha256(der).hexdigest())
        # OpenSSL parses the leading certificate and ignores the trailing bytes, so the
        # validator accepts this PEM. The fingerprint would then cover bytes that are
        # not part of the X.509 certificate, which the validator must refuse.
        with self.assertRaises(ValueError):
            _certificate_pem_fingerprint(_pem_from_der(der + b"\x30\x00"))


if __name__ == "__main__":
    unittest.main()
