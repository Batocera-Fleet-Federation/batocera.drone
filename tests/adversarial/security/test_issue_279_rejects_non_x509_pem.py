"""A pairing response must not turn arbitrary DER bytes into a trust pin."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.transfer import peer_connectivity, swarm_membership
from app.transfer.peer_connectivity import PeerPairingRejected


PEER_ID = "bb:bb:bb:bb:02:79"
NON_X509_PEM = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
NON_X509_FINGERPRINT = hashlib.sha256(b"\0\0\0").hexdigest()


class RejectNonX509PairingCertificateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self._tmp.name), device_id="local-drone")
        self.cache_path = peer_connectivity._local_peer_cert_cache_path(self.settings, PEER_ID)

    def test_unpinned_peer_cannot_store_decodable_non_x509_pem_as_trust_pin(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "status": "paired",
                "drone_id": PEER_ID,
                "certificate_pem": NON_X509_PEM,
                # A peer controls both the bootstrap response and this reply on
                # the unpinned HTTP pairing path, so matching hashes are not a
                # substitute for parsing an X.509 certificate.
                "certificate_fingerprint": NON_X509_FINGERPRINT,
            }
        ).encode()
        certificate_manager = mock.MagicMock()
        certificate_manager.return_value.ensure_certificate.return_value = {
            "public_certificate": "-----BEGIN CERTIFICATE-----\nLOCAL\n-----END CERTIFICATE-----\n",
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
                    {
                        "drone_id": PEER_ID,
                        "reachable_url": "http://peer.example:8080",
                        "certificate_fingerprint": NON_X509_FINGERPRINT,
                    },
                    "operator-code",
                )

        self.assertFalse(self.cache_path.exists())
        save_peer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
