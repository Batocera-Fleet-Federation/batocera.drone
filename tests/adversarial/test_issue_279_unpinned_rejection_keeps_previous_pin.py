"""Issue #279 boundary on the unpinned (operator-typed http://) path: rejected answers keep the pin.

On the unpinned path there is no trust anchor until the answer arrives, so the only defence
against a rejected answer overwriting a live pin is the post-save rollback that compares the
offered certificate with the discovery fingerprint. The #279 fix adds an early check for a
missing certificate; this suite proves the rollback still protects an existing pin when the
answer carries a certificate that does not match discovery.

Expected behaviour, derived from the issue and the pin invariants before reading the fix:

* A status=paired answer whose certificate fingerprint does not match the discovery
  fingerprint is a rejected pairing (``PeerPairingRejected``, the 502 mapping).
* The live certificate cache file is restored byte-for-byte; a rejection with no prior pin
  leaves no file behind.
* No paired-peer row is saved for a rejected answer.
* Control: when the offered certificate matches discovery, the pairing is accepted and the
  cache file holds the new certificate.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.transfer import peer_connectivity, swarm_membership
from app.transfer.peer_connectivity import PeerPairingRejected


OPERATOR_ID = "aa:aa:aa:aa:02:79"
PEER_ID = "bb:bb:bb:bb:02:79"
PEER_URL = "http://192.168.1.44:8080"
OFFERED_PEM = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
OFFERED_FINGERPRINT = hashlib.sha256(ssl.PEM_cert_to_DER_cert(OFFERED_PEM)).hexdigest()
MISMATCHED_DISCOVERY = "ee" * 32
PREVIOUS_PIN = "-----BEGIN CERTIFICATE-----\nPREVIOUSLY-PINNED\n-----END CERTIFICATE-----\n"


def _response(body: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(body).encode("utf-8")
    return response


class UnpinnedRejectionKeepsPreviousPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self._tmp.name).resolve(), device_id=OPERATOR_ID)
        # The cache file name is sanitised from the peer id (colons become underscores), so use
        # the product's own path helper; a hand-built name would point at a file nothing writes.
        self.cache_path = peer_connectivity._local_peer_cert_cache_path(self.settings, PEER_ID)
        self.cache_dir = self.cache_path.parent
        patches = [
            mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True),
            mock.patch.object(peer_connectivity._local_network, "is_local_mode", return_value=True),
            mock.patch.object(peer_connectivity._local_network, "get_paired_peer", return_value={}),
            mock.patch.object(peer_connectivity._local_network, "discovery_payload", return_value={}),
            mock.patch.object(peer_connectivity._local_network, "save_paired_peer"),
            mock.patch.object(peer_connectivity, "DroneCertificateManager"),
            mock.patch.object(peer_connectivity, "_drone_scheme", return_value="http"),
            mock.patch.object(peer_connectivity, "_drone_advertised_api_port", return_value=8080),
            mock.patch.object(peer_connectivity, "_drone_advertised_peer_mtls_port", return_value=8080),
            mock.patch.object(swarm_membership, "record_approved_member"),
            mock.patch.object(swarm_membership, "propagate_membership"),
        ]
        mocks = []
        for patcher in patches:
            mocks.append(patcher.start())
            self.addCleanup(patcher.stop)
        mocks[5].return_value.ensure_certificate.return_value = {
            "public_certificate": "-----BEGIN CERTIFICATE-----\nLOCAL\n-----END CERTIFICATE-----\n",
            "fingerprint": "cd" * 32,
        }
        self.save_paired_peer = mocks[4]

    def _pair(self, discovery_fingerprint: str) -> dict:
        peer = {
            "drone_id": PEER_ID,
            "reachable_url": PEER_URL,
            "certificate_fingerprint": discovery_fingerprint,
        }
        body = {"status": "paired", "drone_id": PEER_ID, "certificate_pem": OFFERED_PEM}
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response(body)):
            return peer_connectivity._local_pair_peer(self.settings, peer, "operator-code")

    def test_mismatched_offer_restores_the_previous_pin_byte_for_byte(self) -> None:
        self.cache_dir.mkdir(parents=True)
        self.cache_path.write_text(PREVIOUS_PIN, encoding="utf-8")
        with self.assertRaises(PeerPairingRejected) as raised:
            self._pair(MISMATCHED_DISCOVERY)
        self.assertEqual(str(raised.exception), "paired peer certificate fingerprint did not match discovery")
        self.assertEqual(self.cache_path.read_text(encoding="utf-8"), PREVIOUS_PIN)
        self.save_paired_peer.assert_not_called()

    def test_mismatched_offer_without_a_prior_pin_leaves_no_file(self) -> None:
        with self.assertRaises(PeerPairingRejected):
            self._pair(MISMATCHED_DISCOVERY)
        self.assertFalse(self.cache_path.exists(), "a rejected offer must not leave a trust pin behind")
        self.save_paired_peer.assert_not_called()

    def test_control_matching_offer_is_pinned_and_saved(self) -> None:
        self.cache_dir.mkdir(parents=True)
        self.cache_path.write_text(PREVIOUS_PIN, encoding="utf-8")
        self._pair(OFFERED_FINGERPRINT)
        self.assertEqual(self.cache_path.read_text(encoding="utf-8"), OFFERED_PEM)
        self.save_paired_peer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
