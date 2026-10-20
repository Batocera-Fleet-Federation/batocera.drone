"""Issue #279 adversarial matrix: a paired answer whose certificate carries no usable certificate.

Driven at the ``_local_pair_peer`` seam with ``urlopen`` scripted, on the unpinned
(operator-typed http://) path and the pinned (https with trust anchor) path, with the real
certificate-cache save step underneath.

Expected behaviour, derived from the issue (a 200 ``status=paired`` answer must never surface
as a client 400) and the pairing invariants before reading the fix:

* Any ``certificate_pem`` that does not yield certificate material is a rejected pairing:
  ``PeerPairingRejected``, never ``ValueError``.
* A rejected answer writes nothing: no trust pin file, no paired-peer row, and a previously
  pinned certificate file stays byte-identical.
* Malformed-envelope bodies (marker present, header/footer/encoding wrong) are rejected on
  both paths.
* Envelope-shaped bodies with zero decodable base64 bytes are rejected on both paths. These
  decode to an empty DER, which would otherwise be pinned as ``sha256(b"")``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.transfer import peer_connectivity
from app.transfer.peer_connectivity import PeerPairingRejected


OPERATOR_ID = "aa:aa:aa:aa:02:79"
PEER_ID = "bb:bb:bb:bb:02:79"
PINNED_FINGERPRINT = "ab" * 32
PREVIOUS_PIN = "-----BEGIN CERTIFICATE-----\nPREVIOUSLY-PINNED\n-----END CERTIFICATE-----\n"
REJECTION = "paired peer returned an unreadable certificate"

# Marker present, but the PEM envelope or encoding is wrong: the save step raises ValueError.
MALFORMED_ENVELOPE = {
    "bare-marker": "BEGIN CERTIFICATE",
    "no-footer": "-----BEGIN CERTIFICATE-----\nAAAA\n",
    "no-header": "BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----",
    "leading-junk": "junk\n-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
    "non-ascii-body": "-----BEGIN CERTIFICATE-----\nÄÖ\n-----END CERTIFICATE-----\n",
}

# Correct envelope, but no base64 content: these decode to zero bytes and must not become a pin.
ZERO_LENGTH_BODY = {
    "empty-body": "-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n",
    "padding-only": "-----BEGIN CERTIFICATE-----\n====\n-----END CERTIFICATE-----\n",
    "symbols-only": "-----BEGIN CERTIFICATE-----\n@@@@\n-----END CERTIFICATE-----\n",
    "non-base64-letters-only": "-----BEGIN CERTIFICATE-----\n!!!!\n-----END CERTIFICATE-----\n",
}


def _response(body: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(body).encode("utf-8")
    return response


class PairingAnswerShapeMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self._tmp.name).resolve(), device_id=OPERATOR_ID)
        self.cache_dir = self.settings.userdata_root / "system" / "drone-app" / "local-peer-certs"
        self.cache_path = self.cache_dir / f"{PEER_ID}.crt"

    def _pair(self, certificate_pem, *, pinned: bool) -> None:
        pinned_record = {"certificate_fingerprint": PINNED_FINGERPRINT} if pinned else {}
        url = "https://peer.example:8443" if pinned else "http://peer.example:8080"
        peer = {"drone_id": PEER_ID, "reachable_url": url}
        body = {"status": "paired", "drone_id": PEER_ID, "certificate_pem": certificate_pem}
        patches = [
            mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True),
            mock.patch.object(peer_connectivity._local_network, "is_local_mode", return_value=True),
            mock.patch.object(peer_connectivity._local_network, "get_paired_peer", return_value=pinned_record),
            mock.patch.object(peer_connectivity._local_network, "discovery_payload", return_value={}),
            mock.patch.object(peer_connectivity._local_network, "save_paired_peer"),
            mock.patch.object(peer_connectivity, "DroneCertificateManager"),
            mock.patch.object(peer_connectivity, "_drone_scheme", return_value="https" if pinned else "http"),
            mock.patch.object(peer_connectivity, "_drone_advertised_api_port", return_value=8080),
            mock.patch.object(peer_connectivity, "_drone_advertised_peer_mtls_port", return_value=8080),
            mock.patch.object(peer_connectivity, "_pairing_tls_context", return_value=mock.MagicMock()),
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
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response(body)):
            return peer_connectivity._local_pair_peer(self.settings, peer, "operator-code")

    def _assert_rejected_without_writes(self, certificate_pem, *, pinned: bool) -> None:
        with self.assertRaises(PeerPairingRejected) as raised:
            self._pair(certificate_pem, pinned=pinned)
        self.assertNotIsInstance(raised.exception, ValueError)
        self.assertFalse(self.cache_path.exists(), "a rejected answer must not create a trust pin")
        self.save_paired_peer.assert_not_called()

    def test_malformed_envelope_bodies_are_rejected_on_both_paths(self) -> None:
        for pinned in (False, True):
            for label, certificate_pem in MALFORMED_ENVELOPE.items():
                with self.subTest(pinned=pinned, case=label):
                    self._assert_rejected_without_writes(certificate_pem, pinned=pinned)

    def test_malformed_envelope_rejection_keeps_a_previously_pinned_certificate(self) -> None:
        self.cache_dir.mkdir(parents=True)
        self.cache_path.write_text(PREVIOUS_PIN, encoding="utf-8")
        for pinned in (False, True):
            for label, certificate_pem in MALFORMED_ENVELOPE.items():
                with self.subTest(pinned=pinned, case=label):
                    with self.assertRaises(PeerPairingRejected):
                        self._pair(certificate_pem, pinned=pinned)
                    self.assertEqual(self.cache_path.read_text(encoding="utf-8"), PREVIOUS_PIN)
                    self.save_paired_peer.assert_not_called()

    def test_zero_length_certificate_bodies_are_rejected_on_the_pinned_path(self) -> None:
        for label, certificate_pem in ZERO_LENGTH_BODY.items():
            with self.subTest(case=label):
                self._assert_rejected_without_writes(certificate_pem, pinned=True)

    def test_zero_length_certificate_bodies_are_not_pinned_on_the_unpinned_path(self) -> None:
        # Blocking #279 finding: the marker gate passes these, the save step decodes them to
        # zero bytes and pins sha256(b""). The peer returned no certificate, so nothing is pinned.
        for label, certificate_pem in ZERO_LENGTH_BODY.items():
            with self.subTest(case=label):
                self._assert_rejected_without_writes(certificate_pem, pinned=False)


if __name__ == "__main__":
    unittest.main()
