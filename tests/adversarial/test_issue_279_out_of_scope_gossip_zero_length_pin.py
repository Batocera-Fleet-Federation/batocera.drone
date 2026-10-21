"""Out-of-scope evidence for #279: swarm gossip pins a zero-length certificate.

#279 is about pair-by-address answers. This is the same boundary on a different path: the
swarm membership ``_apply_add`` accepts a gossip card whose ``certificate_pem`` carries the
markers but no certificate bytes. The body decodes to zero bytes, so the card is saved and
pinned under ``sha256(b"")`` with no cross-check against any advertised fingerprint.

The control case proves the test reaches the save step (a decodable body is saved), so the
zero-length assertion is not passing just because an earlier guard returned False.

Kept in a separate suite so it does not block #279 delivery.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.transfer import swarm_membership
from pairing_certificate_fixture import REAL_CERT_PEM


PEER_ID = "bb:bb:bb:bb:02:79"
ZERO_LENGTH_PEM = "-----BEGIN CERTIFICATE-----\n!!!!\n-----END CERTIFICATE-----\n"
# A real certificate: it parses as X.509, so it passes the guards and reaches the save step.
DECODABLE_PEM = REAL_CERT_PEM


def _record(pem: str) -> dict:
    card = {
        "certificate_pem": pem,
        "scheme": "http",
        "reachable_url": "http://peer.example:8080",
        "advertised_reachable_url": "http://peer.example:8080",
    }
    return {"peer_id": PEER_ID, "op": "add", "epoch": 1, "origin": "test", "card": card}


class GossipZeroLengthPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self._tmp.name).resolve(), device_id="aa:aa:aa:aa:02:79")

    def _apply(self, pem: str) -> tuple[bool, mock.MagicMock]:
        state = {"clock": 0, "records": {}}
        patches = [
            mock.patch.object(swarm_membership._local_network, "get_paired_peer", return_value={}),
            mock.patch.object(swarm_membership._local_network, "save_paired_peer"),
            mock.patch.object(swarm_membership, "_activate_certificate"),
            mock.patch.object(swarm_membership._identity, "own_certificate_fingerprint", return_value="cd" * 32),
        ]
        mocks = [patcher.start() for patcher in patches]
        for patcher in patches:
            self.addCleanup(patcher.stop)
        applied = swarm_membership._apply_add(self.settings, state, _record(pem))
        return applied, mocks[1]

    def test_control_decodable_card_reaches_the_save_step(self) -> None:
        applied, save = self._apply(DECODABLE_PEM)
        self.assertTrue(applied)
        save.assert_called_once()

    def test_zero_length_gossip_certificate_is_not_saved_or_pinned(self) -> None:
        applied, save = self._apply(ZERO_LENGTH_PEM)
        self.assertFalse(applied, "a gossip card with no certificate bytes must not add a paired peer")
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
