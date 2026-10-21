"""Issue #279 boundaries: a paired 200 without a usable certificate is a rejected pairing.

Driven at the ``_local_pair_peer`` seam with ``urlopen`` scripted, so the pinned (HTTPS
with a trust anchor) and unpinned (operator-typed http://) paths are both exercised with
the real certificate-cache save step. Expected behaviour, derived from the issue and the
pairing invariants before reading the fix:

* ``status=paired`` with a matching drone_id but no certificate, or one that is not a
  PEM-bearing string, raises ``PeerPairingRejected`` (the 502 mapping), never ``ValueError``.
* A rejected answer never overwrites or removes the certificate cache file, pinned or not.
* No paired-peer row is written for a rejected answer.
* The peer's body text is never echoed into the rejection message.
* Control: a response that carries the certificate marker still reaches the save step.
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
from pairing_certificate_fixture import REAL_CERT_FINGERPRINT, REAL_CERT_PEM


OPERATOR_ID = "aa:aa:aa:aa:02:79"
PEER_ID = "bb:bb:bb:bb:02:79"
PINNED_FINGERPRINT = "ab" * 32
LEAK = "issue-279-peer-body-must-not-echo"
PREVIOUS_PIN = "-----BEGIN CERTIFICATE-----\nPREVIOUSLY-PINNED\n-----END CERTIFICATE-----\n"


def _response(body: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(body).encode("utf-8")
    return response


class MissingCertificateBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self._tmp.name).resolve(), device_id=OPERATOR_ID)
        self.cache_dir = self.settings.userdata_root / "system" / "drone-app" / "local-peer-certs"
        self.cache_path = self.cache_dir / f"{PEER_ID}.crt"

    def _pair(self, body: dict, *, pinned: bool, url: str = "http://peer.example:8080") -> None:
        pinned_record = {"certificate_fingerprint": PINNED_FINGERPRINT} if pinned else {}
        if pinned:
            url = "https://peer.example:8443"
        peer = {"drone_id": PEER_ID, "reachable_url": url}
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
        cert_manager = mocks[5]
        cert_manager.return_value.ensure_certificate.return_value = {
            "public_certificate": "-----BEGIN CERTIFICATE-----\nLOCAL\n-----END CERTIFICATE-----\n",
            "fingerprint": "cd" * 32,
        }
        self.save_paired_peer = mocks[4]
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response(body)):
            # The unpinned path needs the operator-confirmed anchor before anything is sent.
            return peer_connectivity._local_pair_peer(
                self.settings,
                peer,
                "operator-code",
                confirmed_fingerprint="" if pinned else REAL_CERT_FINGERPRINT,
            )

    def _assert_rejected_without_writes(self, body: dict, *, pinned: bool) -> PeerPairingRejected:
        with self.assertRaises(PeerPairingRejected) as raised:
            self._pair(body, pinned=pinned)
        self.assertFalse(self.cache_path.exists(), "a rejected answer must not create a trust pin")
        self.save_paired_peer.assert_not_called()
        return raised.exception

    def test_missing_or_empty_certificate_is_rejected_on_both_paths(self) -> None:
        cases = (
            ("absent", {"status": "paired", "drone_id": PEER_ID}),
            ("null", {"status": "paired", "drone_id": PEER_ID, "certificate_pem": None}),
            ("empty", {"status": "paired", "drone_id": PEER_ID, "certificate_pem": ""}),
            ("blank", {"status": "paired", "drone_id": PEER_ID, "certificate_pem": "   \n  "}),
        )
        for pinned in (False, True):
            for label, body in cases:
                with self.subTest(pinned=pinned, case=label):
                    error = self._assert_rejected_without_writes(body, pinned=pinned)
                    self.assertEqual(str(error), "paired peer did not return a certificate")

    def test_non_string_certificate_values_are_rejected_not_coerced_into_a_pin(self) -> None:
        cases = (
            ("list-with-marker", ["-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"]),
            ("dict-with-marker", {"pem": "BEGIN CERTIFICATE"}),
            ("number", 0),
            ("boolean-true", True),
            ("empty-list", []),
        )
        for label, value in cases:
            with self.subTest(case=label):
                body = {"status": "paired", "drone_id": PEER_ID, "certificate_pem": value}
                self._assert_rejected_without_writes(body, pinned=False)

    def test_rejected_answer_keeps_a_previously_pinned_certificate(self) -> None:
        self.cache_dir.mkdir(parents=True)
        self.cache_path.write_text(PREVIOUS_PIN, encoding="utf-8")
        for pinned in (False, True):
            with self.subTest(pinned=pinned):
                with self.assertRaises(PeerPairingRejected):
                    self._pair({"status": "paired", "drone_id": PEER_ID}, pinned=pinned)
                self.assertEqual(self.cache_path.read_text(encoding="utf-8"), PREVIOUS_PIN)
                self.save_paired_peer.assert_not_called()

    def test_peer_body_text_is_never_echoed_into_the_rejection(self) -> None:
        error = self._assert_rejected_without_writes(
            {"status": "paired", "drone_id": PEER_ID, "certificate_pem": LEAK, "name": LEAK},
            pinned=False,
        )
        self.assertNotIn(LEAK, str(error))

    def test_control_certificate_marker_still_reaches_the_save_step(self) -> None:
        # The new gate must only reject answers without a certificate, not a valid one.
        with mock.patch.object(
            peer_connectivity, "_save_local_peer_certificate", side_effect=RuntimeError("reached save")
        ) as save:
            with self.assertRaisesRegex(RuntimeError, "reached save"):
                self._pair(
                    {
                        "status": "paired",
                        "drone_id": PEER_ID,
                        "certificate_pem": REAL_CERT_PEM,
                    },
                    pinned=False,
                )
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
