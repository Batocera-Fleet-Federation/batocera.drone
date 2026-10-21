"""Issue #279 adversarial UAT: a certificate-less paired answer is a retryable rejection.

Expected behaviour, derived from the issue and the pairing invariants before reading the fix:

* A 200 with ``status=paired`` and a matching drone id but no certificate is a rejected
  pairing (``PeerPairingRejected``, mapped to 502), never a ``ValueError`` (400).
* The rejection leaves nothing behind: no trust pin, no paired-peer row, no approved
  membership record, no gossip, and no peer certificate loaded into the server.
* The operator can simply try again. A later answer that carries a real certificate pairs
  normally on both the unpinned (operator-typed http) and pinned (https) paths: the pin is
  written, membership is recorded and gossiped exactly once, and the certificate is loaded.
  The earlier rejection must not block or corrupt that retry.
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
from app.web import handlers_network
from pairing_certificate_fixture import REAL_CERT_FINGERPRINT as REAL_FINGERPRINT
from pairing_certificate_fixture import REAL_CERT_PEM as REAL_PEM


OPERATOR_ID = "aa:aa:aa:aa:27:90"
PEER_ID = "bb:bb:bb:bb:27:90"
PREVIOUS_PEM = "-----BEGIN CERTIFICATE-----\nPREVIOUSLY-PINNED\n-----END CERTIFICATE-----\n"


def _response(body: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(body).encode("utf-8")
    return response


class CertificatelessRetryUAT(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self._tmp.name).resolve(), device_id=OPERATOR_ID)
        self.cache_path = peer_connectivity._local_peer_cert_cache_path(self.settings, PEER_ID)

        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = self.settings
        self.handler._send_json = mock.Mock()

        self.stored = {"drone_id": PEER_ID, "certificate_path": str(self.cache_path)}
        self.get_paired_peer = self._patch(peer_connectivity._local_network, "get_paired_peer", return_value={})
        self.save_paired_peer = self._patch(
            peer_connectivity._local_network, "save_paired_peer", return_value=self.stored
        )
        self._patch(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True)
        self._patch(peer_connectivity._local_network, "is_local_mode", return_value=True)
        self._patch(peer_connectivity._local_network, "discovery_payload", return_value={})
        self._patch(peer_connectivity, "_drone_scheme", return_value="http")
        self._patch(peer_connectivity, "_drone_advertised_api_port", return_value=8080)
        self._patch(peer_connectivity, "_drone_advertised_peer_mtls_port", return_value=8080)
        cert_manager = self._patch(peer_connectivity, "DroneCertificateManager")
        cert_manager.return_value.ensure_certificate.return_value = {
            "public_certificate": "-----BEGIN CERTIFICATE-----\nLOCAL\n-----END CERTIFICATE-----\n",
            "fingerprint": "cd" * 32,
        }
        self.record_approved_member = self._patch(swarm_membership, "record_approved_member")
        self.propagate_membership = self._patch(swarm_membership, "propagate_membership")
        self.load_peer_cert_everywhere = self._patch(handlers_network, "load_peer_cert_everywhere")

    def _patch(self, target: object, name: str, **kwargs) -> mock.MagicMock:
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _attempt(self, body: dict, *, url: str = "http://peer.example:8080", confirmed: str = REAL_FINGERPRINT) -> object:
        # The default is the operator-confirmed anchor an unpinned http:// attempt needs.
        peer = {"drone_id": PEER_ID, "reachable_url": url}
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response(body)):
            return self.handler._pair_peer_or_reply(peer, "operator-code", confirmed)

    def test_first_certificateless_answer_leaves_no_side_effects(self) -> None:
        self.assertIsNone(self._attempt({"status": "paired", "drone_id": PEER_ID}))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"], "That drone did not accept the pairing request.")
        self.assertFalse(self.cache_path.exists())
        self.save_paired_peer.assert_not_called()
        self.record_approved_member.assert_not_called()
        self.propagate_membership.assert_not_called()
        self.load_peer_cert_everywhere.assert_not_called()

    def test_retry_with_a_real_certificate_pairs_after_an_unpinned_rejection(self) -> None:
        self.assertIsNone(self._attempt({"status": "paired", "drone_id": PEER_ID}))

        paired = self._attempt({"status": "paired", "drone_id": PEER_ID, "certificate_pem": REAL_PEM})

        self.assertIs(paired, self.stored)
        self.assertEqual(self.cache_path.read_text(encoding="utf-8"), REAL_PEM)
        self.save_paired_peer.assert_called_once()
        self.record_approved_member.assert_called_once_with(self.settings, self.stored)
        self.propagate_membership.assert_called_once_with(self.settings)
        # Only the rejected attempt answered the handler; the successful one is for the caller to reply to.
        self.assertEqual(self.handler._send_json.call_count, 1)

    def test_retry_with_a_real_certificate_pairs_after_a_pinned_rejection(self) -> None:
        pin = REAL_FINGERPRINT
        self.get_paired_peer.return_value = {"certificate_fingerprint": pin}
        self.cache_path.parent.mkdir(parents=True)
        self.cache_path.write_text(PREVIOUS_PEM, encoding="utf-8")
        tls_context = self._patch(peer_connectivity, "_pairing_tls_context", return_value=mock.MagicMock())
        url = "https://peer.example:8443"

        self.assertIsNone(self._attempt({"status": "paired", "drone_id": PEER_ID}, url=url, confirmed=pin))
        self.assertEqual(self.cache_path.read_text(encoding="utf-8"), PREVIOUS_PEM)
        self.save_paired_peer.assert_not_called()
        self.propagate_membership.assert_not_called()

        paired = self._attempt(
            {"status": "paired", "drone_id": PEER_ID, "certificate_pem": REAL_PEM}, url=url, confirmed=pin
        )

        self.assertIs(paired, self.stored)
        tls_context.assert_called_with("https://peer.example:8443", pin)
        self.assertEqual(self.cache_path.read_text(encoding="utf-8"), REAL_PEM)
        self.save_paired_peer.assert_called_once()
        self.propagate_membership.assert_called_once_with(self.settings)

    def test_rejected_answer_does_not_block_a_later_answer_that_omits_the_fingerprint(self) -> None:
        # A certificate-less first answer must not leave a half-written pin that makes a
        # later answer look like a fingerprint mismatch.
        self.assertIsNone(self._attempt({"status": "paired", "drone_id": PEER_ID}))
        self.assertFalse(self.cache_path.parent.exists())

        paired = self._attempt(
            {"status": "paired", "drone_id": PEER_ID, "certificate_pem": REAL_PEM, "certificate_fingerprint": REAL_FINGERPRINT}
        )

        self.assertIs(paired, self.stored)
        self.assertEqual(self.cache_path.read_text(encoding="utf-8"), REAL_PEM)


if __name__ == "__main__":
    unittest.main()
