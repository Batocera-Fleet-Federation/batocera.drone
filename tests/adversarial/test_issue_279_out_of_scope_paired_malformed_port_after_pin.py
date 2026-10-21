"""Out-of-scope evidence for #279: a paired answer with a malformed port fails after its pin is written.

#279 is about a status=paired answer with no certificate. This is a sibling payload: the
certificate is valid and matches discovery, so the save step writes the trust pin, and then
the paired-record assembly calls ``int(result["api_port"])`` on a non-numeric value. That
``ValueError`` is not caught by the pairing helper and do_POST maps it to HTTP 400, the same
client-error mapping #279 removes for the certificate-less case. It also leaves a trust pin
on disk with no paired-peer row.

The control case uses a numeric port and proves the test reaches the save step.

Kept in a separate suite so it does not block #279 delivery.
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
from pairing_certificate_fixture import REAL_CERT_FINGERPRINT, REAL_CERT_PEM


OPERATOR_ID = "aa:aa:aa:aa:02:79"
PEER_ID = "bb:bb:bb:bb:02:79"
PEER_URL = "http://192.168.1.45:8080"
OFFERED_PEM = REAL_CERT_PEM
OFFERED_FINGERPRINT = REAL_CERT_FINGERPRINT


def _response(body: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(body).encode("utf-8")
    return response


class MalformedPortAfterPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self._tmp.name).resolve(), device_id=OPERATOR_ID)
        # Use the product's own path helper: the file name is sanitised from the peer id.
        self.cache_path = peer_connectivity._local_peer_cert_cache_path(self.settings, PEER_ID)
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

    def _pair(self, api_port: object) -> None:
        peer = {"drone_id": PEER_ID, "reachable_url": PEER_URL, "certificate_fingerprint": OFFERED_FINGERPRINT}
        body = {
            "status": "paired",
            "drone_id": PEER_ID,
            "certificate_pem": OFFERED_PEM,
            "api_port": api_port,
        }
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response(body)):
            peer_connectivity._local_pair_peer(
                self.settings,
                peer,
                "operator-code",
                confirmed_fingerprint=OFFERED_FINGERPRINT,
            )

    def test_malformed_port_is_a_rejected_pairing_that_leaves_no_pin(self) -> None:
        try:
            self._pair("not-a-port")
        except Exception as error:  # noqa: BLE001 - the assertion below names the expected class
            raised = error
        else:
            self.fail("a paired answer with a non-numeric port must be rejected")
        self.assertIsInstance(raised, PeerPairingRejected)
        self.assertFalse(self.cache_path.exists(), "a rejected answer must not leave a trust pin on disk")
        self.save_paired_peer.assert_not_called()

    def test_control_numeric_port_reaches_the_save_step(self) -> None:
        self._pair(8443)
        self.save_paired_peer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
