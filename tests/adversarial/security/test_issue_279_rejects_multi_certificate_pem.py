"""A pairing response must pin exactly one certificate, never a bundle of several."""

from __future__ import annotations

import json
import ssl
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.transfer import peer_connectivity, swarm_membership
from app.transfer.peer_connectivity import PeerPairingRejected


PEER_ID = "bb:bb:bb:bb:02:79"
# The operator-confirmed anchor an unpinned http:// pairing needs before anything is sent.
OPERATOR_CONFIRMED_FINGERPRINT = "cd" * 32
# Two distinct, valid self-signed fixture certificates (test-only keys, discarded).
# Their concatenation passes the single-certificate fingerprint check because the
# decoded base64 happens to align, so the stored pin carries two trust anchors.
FIXTURE_CERT_A = """-----BEGIN CERTIFICATE-----
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
FIXTURE_CERT_B = """-----BEGIN CERTIFICATE-----
MIICpjCCAY4CCQDhoR4CVIVlMDANBgkqhkiG9w0BAQsFADAUMRIwEAYDVQQDDAlm
aXh0dXJlLTEwIBcNMjYxMDE2MDQ0NzU5WhgPMjEyNjA5MjIwNDQ3NTlaMBQxEjAQ
BgNVBAMMCWZpeHR1cmUtMTCCASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEB
AKib1vu/7GjWu3oU+uvupRSPL2HR8+/2A0/PJQHLx7ac7cA34bL/yFxTdICrZaJL
OUC8bIdjM4nbLutQ3Rn/JJJojn4YtzkUvTYHJuN7a2QFpxPQH8IKAYBegJdwQHtQ
3j2bwHkaD73zFylYwywE6VQtaMVbNJl2DzxC5ZbojHS1x2cn/i2oLG31Exyde7lb
61hzS64V1w7gpy132/Eokv/P6j27gQhpc1nYgET+6VHVfxh1QQvd1XuSUmP4j4jb
T12Os3KLZncnruMRYgXSloIELw8OnTr+MjR/Ga0aMJaLqf4furb5egjVJMUv4fVK
6Ptydg4Jh8k924vvAbikIm0CAwEAATANBgkqhkiG9w0BAQsFAAOCAQEAEsUDMz4i
GsPqS8k2jYyjSckzz7ltZnp8H7IjQ41Z5fxm8s2+uVhTaag1NHPgKoEseajqbMvA
cxMFE/0n00Aim517F8xWTkSUayfjSN/1FeFCGJaHoOwr1OqKIFhRKuJB06CuiszJ
oL+FaacvOj8muKCNjoKegl3okgVweFWJP/DKaZOkmay0f9GbIehiryVcazs8ID47
E0iZeemgS+eFeutbTZwj0YlXTWDGq0gM/uclfhMqveOq2Uh+Sta9FKrnBo7p2WH+
7ZAtrG4LYzlaSBoHtY2StYqmfYjuPyfDVmeZXE81cZLGXLEm7/+SAQRC6sQEg/OI
raeB92Ketc/Afg==
-----END CERTIFICATE-----
"""
BUNDLE = FIXTURE_CERT_A + FIXTURE_CERT_B


class RejectMultiCertificatePemTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self._tmp.name), device_id="local-drone")
        self.cache_path = peer_connectivity._local_peer_cert_cache_path(self.settings, PEER_ID)

    def test_bundle_is_not_a_single_certificate_fingerprint(self) -> None:
        # Each fixture is a valid certificate on its own.
        self.assertTrue(peer_connectivity._certificate_pem_fingerprint(FIXTURE_CERT_A))
        # A bundle must be refused, not fingerprinted as if it were one certificate.
        with self.assertRaises(ValueError):
            peer_connectivity._certificate_pem_fingerprint(BUNDLE)

    def test_unpinned_pairing_with_bundle_is_rejected_and_nothing_is_stored(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"status": "paired", "drone_id": PEER_ID, "certificate_pem": BUNDLE}
        ).encode()
        certificate_manager = mock.MagicMock()
        certificate_manager.return_value.ensure_certificate.return_value = {
            "public_certificate": FIXTURE_CERT_B,
            "fingerprint": "cd" * 32,
        }
        with (
            mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True),
            mock.patch.object(peer_connectivity._local_network, "is_local_mode", return_value=True),
            mock.patch.object(peer_connectivity._local_network, "get_paired_peer", return_value={}),
            mock.patch.object(peer_connectivity._local_network, "discovery_payload", return_value={}),
            mock.patch.object(peer_connectivity._local_network, "save_paired_peer") as save_peer,
            mock.patch.object(peer_connectivity, "DroneCertificateManager", certificate_manager),
            mock.patch.object(peer_connectivity, "_drone_scheme", return_value="http"),
            mock.patch.object(peer_connectivity, "_drone_advertised_api_port", return_value=8080),
            mock.patch.object(peer_connectivity, "_drone_advertised_peer_mtls_port", return_value=8080),
            mock.patch.object(peer_connectivity, "urlopen", return_value=response),
            mock.patch.object(swarm_membership, "record_approved_member"),
            mock.patch.object(swarm_membership, "propagate_membership"),
        ):
            with self.assertRaises(PeerPairingRejected):
                peer_connectivity._local_pair_peer(
                    self.settings,
                    {"drone_id": PEER_ID, "reachable_url": "http://peer.example:8080"},
                    "operator-code",
                    confirmed_fingerprint=OPERATOR_CONFIRMED_FINGERPRINT,
                )

        self.assertFalse(self.cache_path.exists())
        save_peer.assert_not_called()

    def test_single_certificate_pin_file_trusts_exactly_one_anchor(self) -> None:
        # Guard the invariant the stored pin relies on: the cache file is used as
        # the CA bundle for later peer requests, so it must hold one certificate.
        with self.assertRaises(ValueError):
            peer_connectivity._save_local_peer_certificate(self.settings, PEER_ID, BUNDLE)
        self.assertFalse(self.cache_path.exists())
        cert_path, _ = peer_connectivity._save_local_peer_certificate(self.settings, PEER_ID, FIXTURE_CERT_A)
        self.assertEqual(len(ssl.create_default_context(cafile=str(cert_path)).get_ca_certs()), 1)


if __name__ == "__main__":
    unittest.main()
