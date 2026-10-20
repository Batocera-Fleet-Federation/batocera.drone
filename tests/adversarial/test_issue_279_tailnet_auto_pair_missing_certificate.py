"""Issue #279 boundary on the tailnet auto-pair path: a paired 200 without a certificate.

The tailnet caller (``handlers_network``) passes ``tailnet_auto_pair=True`` with no pinned
record and no operator-confirmed fingerprint, so the handshake uses the unverified context
and nothing anchors the peer's certificate. That is the path where a certificate-less
"paired" answer is most tempting to accept, so it is exercised separately from the pinned
and operator-typed paths in ``test_issue_279_missing_certificate_boundaries``.

Expected behaviour, derived from the issue (a status=paired answer without a certificate is
a rejected pairing, 502, never a ValueError/400) and the pin invariants before reading the fix:

* ``PeerPairingRejected`` with the fixed message; the tailnet caller turns that into a
  ``tailnet_pair_error`` string rather than a crash.
* No trust pin file is created and no paired-peer row is saved.
* A valid certificate on the same path still reaches the save step (control).
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
TAILNET_URL = "https://100.64.0.9:8443"
VALID_PEM = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
FIXED_REJECTION = "paired peer did not return a certificate"


def _response(body: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(body).encode("utf-8")
    return response


class TailnetAutoPairMissingCertificateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self._tmp.name).resolve(), device_id=OPERATOR_ID)
        # Use the product's own path helper: the file name is sanitised from the peer id.
        self.cache_path = peer_connectivity._local_peer_cert_cache_path(self.settings, PEER_ID)
        patches = [
            mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True),
            mock.patch.object(peer_connectivity._local_network, "is_local_mode", return_value=True),
            # Tailnet auto-pair has no prior pin: the caller only knows the tailnet address.
            mock.patch.object(peer_connectivity._local_network, "get_paired_peer", return_value={}),
            mock.patch.object(peer_connectivity._local_network, "discovery_payload", return_value={}),
            mock.patch.object(peer_connectivity._local_network, "save_paired_peer"),
            mock.patch.object(peer_connectivity, "DroneCertificateManager"),
            mock.patch.object(peer_connectivity, "_drone_scheme", return_value="https"),
            mock.patch.object(peer_connectivity, "_drone_advertised_api_port", return_value=8443),
            mock.patch.object(peer_connectivity, "_drone_advertised_peer_mtls_port", return_value=8443),
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

    def _pair(self, body: dict) -> dict:
        peer = {"drone_id": PEER_ID, "reachable_url": TAILNET_URL, "pairing_source": "tailnet"}
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response(body)) as opened:
            stored = peer_connectivity._local_pair_peer(
                self.settings, peer, "", tailnet_auto_pair=True
            )
        # The tailnet path must actually have used the unverified tailnet context, not a pinned one.
        self.assertTrue(opened.called)
        return stored

    def test_certificate_less_paired_answer_is_rejected_without_pin_or_row(self) -> None:
        bodies = (
            {"status": "paired", "drone_id": PEER_ID},
            {"status": "paired", "drone_id": PEER_ID, "certificate_pem": ""},
            {"status": "paired", "drone_id": PEER_ID, "certificate_pem": None},
        )
        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaises(PeerPairingRejected) as raised:
                    self._pair(body)
                self.assertEqual(str(raised.exception), FIXED_REJECTION)
                self.assertFalse(self.cache_path.exists(), "tailnet rejection must not pin a certificate")
                self.save_paired_peer.assert_not_called()

    def test_bare_marker_without_envelope_is_rejected_as_unreadable_without_pin(self) -> None:
        # The marker text passes the presence check, so the save step decides: it cannot parse
        # the envelope, raises ValueError before writing, and the helper maps that to a rejection.
        with self.assertRaises(PeerPairingRejected) as raised:
            self._pair({"status": "paired", "drone_id": PEER_ID, "certificate_pem": "BEGIN CERTIFICATE"})
        self.assertEqual(str(raised.exception), "paired peer returned an unreadable certificate")
        self.assertFalse(self.cache_path.exists(), "an unreadable certificate must not be pinned")
        self.save_paired_peer.assert_not_called()

    def test_tailnet_caller_sees_the_fixed_reason_not_a_value_error(self) -> None:
        # handlers_network reports any exception as ``str(error)``; the message must be the
        # fixed rejection text, so the operator sees the pairing reason rather than a stack trace.
        with self.assertRaises(PeerPairingRejected) as raised:
            self._pair({"status": "paired", "drone_id": PEER_ID})
        self.assertNotIsInstance(raised.exception, ValueError)
        self.assertNotIn("peer certificate is required", str(raised.exception))

    def test_control_valid_certificate_on_tailnet_path_reaches_the_save_step(self) -> None:
        with mock.patch.object(
            peer_connectivity, "_save_local_peer_certificate", side_effect=RuntimeError("reached save")
        ) as save:
            with self.assertRaisesRegex(RuntimeError, "reached save"):
                self._pair({"status": "paired", "drone_id": PEER_ID, "certificate_pem": VALID_PEM})
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
