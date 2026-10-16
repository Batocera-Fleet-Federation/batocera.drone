"""Issue #279 adversarial boundaries: certificate-marker encodings, and the fix must not over-broaden.

Expected behaviour, derived from the issue before reading the fix:

* Any ``certificate_pem`` that is not a usable PEM certificate is a rejected pairing on both the
  pinned and unpinned paths, whatever the encoding trick: non-ASCII characters, a bare marker
  word, a lower-case marker, or a truncated envelope. None of them may leave a pin on disk.
* Rejection is narrow. A genuine client error that is not about the peer's answer (a discovered
  peer with no reachable address) must still surface as ``ValueError`` so the handler keeps
  answering 400 for it. Mapping every ``ValueError`` to a peer rejection would hide real bugs.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.transfer import peer_connectivity, swarm_membership
from app.transfer.peer_connectivity import PeerPairingRejected


OPERATOR_ID = "aa:aa:aa:aa:27:91"
PEER_ID = "bb:bb:bb:bb:27:91"
PINNED_FINGERPRINT = "ab" * 32

BAD_CERTIFICATES = (
    ("non-ascii-body", "-----BEGIN CERTIFICATE-----\nÄÄÄÄ\n-----END CERTIFICATE-----\n"),
    ("bare-marker-word", "BEGIN CERTIFICATE"),
    ("lower-case-marker", "-----begin certificate-----\nAAAA\n-----end certificate-----\n"),
    ("truncated-envelope", "-----BEGIN CERTIFICATE-----\nAAAA"),
    ("trailing-junk", "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\njunk"),
)


def _response(body: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(body).encode("utf-8")
    return response


class MarkerEncodingBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self._tmp.name).resolve(), device_id=OPERATOR_ID)
        self.cache_path = peer_connectivity._local_peer_cert_cache_path(self.settings, PEER_ID)
        self.get_paired_peer = self._patch(peer_connectivity._local_network, "get_paired_peer", return_value={})
        self.save_paired_peer = self._patch(peer_connectivity._local_network, "save_paired_peer")
        self._patch(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True)
        self._patch(peer_connectivity._local_network, "is_local_mode", return_value=True)
        self._patch(peer_connectivity._local_network, "discovery_payload", return_value={})
        self._patch(peer_connectivity, "_drone_scheme", return_value="http")
        self._patch(peer_connectivity, "_drone_advertised_api_port", return_value=8080)
        self._patch(peer_connectivity, "_drone_advertised_peer_mtls_port", return_value=8080)
        self._patch(swarm_membership, "record_approved_member")
        self._patch(swarm_membership, "propagate_membership")
        cert_manager = self._patch(peer_connectivity, "DroneCertificateManager")
        cert_manager.return_value.ensure_certificate.return_value = {
            "public_certificate": "-----BEGIN CERTIFICATE-----\nLOCAL\n-----END CERTIFICATE-----\n",
            "fingerprint": "cd" * 32,
        }

    def _patch(self, target: object, name: str, **kwargs) -> mock.MagicMock:
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _pair(self, certificate_pem: object, *, pinned: bool) -> None:
        if pinned:
            self.get_paired_peer.return_value = {"certificate_fingerprint": PINNED_FINGERPRINT}
            self._patch(peer_connectivity, "_pairing_tls_context", return_value=mock.MagicMock())
            url = "https://peer.example:8443"
        else:
            url = "http://peer.example:8080"
        peer = {"drone_id": PEER_ID, "reachable_url": url}
        body = {"status": "paired", "drone_id": PEER_ID, "certificate_pem": certificate_pem}
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response(body)):
            peer_connectivity._local_pair_peer(self.settings, peer, "operator-code")

    def test_every_non_certificate_encoding_is_rejected_on_both_paths_without_a_pin(self) -> None:
        for pinned in (False, True):
            for label, certificate_pem in BAD_CERTIFICATES:
                with self.subTest(pinned=pinned, case=label):
                    with self.assertRaises(PeerPairingRejected):
                        self._pair(certificate_pem, pinned=pinned)
                    self.assertFalse(self.cache_path.exists(), "a rejected answer must not leave a pin")
                    self.save_paired_peer.assert_not_called()

    def test_non_ascii_body_is_a_rejection_not_a_unicode_error(self) -> None:
        # UnicodeEncodeError is a ValueError subclass; it must be mapped to the peer rejection,
        # not leak as a raw encoding failure.
        non_ascii = dict(BAD_CERTIFICATES)["non-ascii-body"]
        with self.assertRaises(PeerPairingRejected) as raised:
            self._pair(non_ascii, pinned=False)
        self.assertNotIsInstance(raised.exception, UnicodeError)
        self.assertEqual(str(raised.exception), "paired peer returned an unreadable certificate")

    def test_control_a_peer_without_a_reachable_address_is_still_a_client_error(self) -> None:
        # The narrow ValueError handling must not swallow genuine local/client validation errors.
        with self.assertRaisesRegex(ValueError, "discovered peer has no reachable address"):
            peer_connectivity._local_pair_peer(self.settings, {"drone_id": PEER_ID}, "operator-code")
        self.assertFalse(self.cache_path.exists())
        self.save_paired_peer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
