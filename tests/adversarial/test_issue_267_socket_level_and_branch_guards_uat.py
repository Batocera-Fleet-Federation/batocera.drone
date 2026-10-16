"""Issue #267 adversarial UAT: a held identity touches no network primitive at all.

Expected, derived from the issue and CLAUDE.md ("outbound peer TLS all gated") before
reading the patch:

* While swarm networking is held, pair-by-address performs no DNS lookup, opens no
  socket, starts no TLS handshake and spawns no subprocess: asserted below ``urlopen``
  (which is deliberately NOT patched), so a dial hidden behind a different helper or a
  resolution done before the gate would still fail the test.
* Every branch of ``_local_pair_peer`` (cleartext http, tailnet auto-pair, https with no
  trust anchor, https with an anchor) re-checks the hold immediately before the pairing
  POST: a hold that lands after the entry check, while local material is being prepared,
  must not send the POST (which carries the pairing code and our certificate).
* A held tailnet sync reports the hold per device, dials nothing, and persists no
  discovered/paired peer.
* The refusal is a state: lifting the hold lets the very same call dial again.

State is set through the real ``identity_provisioning`` gate.
"""

from __future__ import annotations

import json
import socket
import unittest
from unittest import mock

from app.common import identity_provisioning
from app.transfer import peer_connectivity
from app.web import handlers_network

FINGERPRINT = "ab" * 32
HOLD_MESSAGE = "peer pairing is held pending Drone identity verification"


def _hold() -> None:
    identity_provisioning._publish(
        identity_provisioning.State.RECOVERY_REQUIRED, "held", "adversarial hold", False
    )


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        identity_provisioning.reset_runtime_state_for_tests()
        self.addCleanup(identity_provisioning.reset_runtime_state_for_tests)

    def _patch(self, target, attribute: str, **kwargs):
        patcher = mock.patch.object(target, attribute, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started


class NoNetworkPrimitiveWhileHeldTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler.settings.device_id = "drone-self"
        self.handler._send_json = mock.Mock()
        self._patch(handlers_network._local_network, "is_local_mode", return_value=True)
        self.save = self._patch(handlers_network._local_network, "save_paired_peer")

    def test_pair_by_address_resolves_and_connects_nothing_while_held(self) -> None:
        _hold()
        # `.invalid` can never resolve, so an unexpected dial is also a visible failure.
        addresses = (
            "peer.invalid", "peer.invalid:8443", "http://peer.invalid:8080",
            "https://peer.invalid", "100.64.0.9", "[fd7a:115c::1]:8443",
        )
        for address in addresses:
            with self.subTest(address=address), \
                    mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS lookup")) as dns, \
                    mock.patch.object(socket, "create_connection", side_effect=AssertionError("connect")) as conn, \
                    mock.patch.object(socket.socket, "connect", side_effect=AssertionError("socket.connect")) as raw, \
                    mock.patch.object(peer_connectivity.subprocess, "run", side_effect=AssertionError("subprocess")) as run, \
                    mock.patch.object(peer_connectivity.subprocess, "Popen", side_effect=AssertionError("Popen")) as popen:
                self.handler._send_json.reset_mock()
                self.handler._handle_admin_local_peer_pair_by_address(
                    {"address": address, "pairing_code": "secret", "certificate_fingerprint": FINGERPRINT}
                )
                self.assertEqual(self.handler._send_json.call_args.args, (503, {"error": HOLD_MESSAGE}))
                for primitive in (dns, conn, raw, run, popen):
                    primitive.assert_not_called()
        self.save.assert_not_called()

    def test_lifting_the_hold_lets_the_same_call_reach_the_network_layer(self) -> None:
        _hold()
        with mock.patch.object(socket, "getaddrinfo", side_effect=OSError("no route")) as dns:
            self.handler._handle_admin_local_peer_pair_by_address({"address": "peer.invalid"})
        dns.assert_not_called()
        self.assertEqual(self.handler._send_json.call_args.args[0], 503)

        identity_provisioning.reset_runtime_state_for_tests()
        self.handler._send_json.reset_mock()
        with mock.patch.object(socket, "getaddrinfo", side_effect=OSError("no route")) as dns:
            self.handler._handle_admin_local_peer_pair_by_address({"address": "peer.invalid"})
        self.assertGreaterEqual(dns.call_count, 1)
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("could not reach a Drone at", payload["error"])


class LocalPairPeerBranchGuardTests(_Case):
    """The hold lands after the entry check, while the request body is being prepared."""

    def _prepare(self) -> mock.MagicMock:
        self._patch(peer_connectivity._local_network, "is_local_mode", return_value=True)
        self._patch(peer_connectivity._local_network, "get_paired_peer", return_value={})
        self._patch(peer_connectivity, "DroneCertificateManager")
        self._patch(peer_connectivity._local_network, "save_paired_peer")
        self._patch(peer_connectivity, "_drone_scheme", return_value="https")
        self._patch(peer_connectivity, "_drone_advertised_api_port", return_value=8443)
        self._patch(peer_connectivity, "_drone_advertised_peer_mtls_port", return_value=8443)
        manager = peer_connectivity.DroneCertificateManager.return_value
        manager.ensure_certificate.return_value = {"public_certificate": "PEM", "fingerprint": FINGERPRINT}
        self._patch(
            peer_connectivity._local_network, "discovery_payload",
            side_effect=lambda *_a, **_k: (_hold(), {"reachable_url": "https://me"})[1],
        )
        return self._patch(peer_connectivity, "urlopen")

    def _assert_held_before_post(self, peer: dict, **kwargs) -> None:
        urlopen_mock = self._prepare()
        with self.assertRaises(peer_connectivity.PeerIdentityHold):
            peer_connectivity._local_pair_peer(mock.MagicMock(device_id="me"), peer, "secret-code", **kwargs)
        urlopen_mock.assert_not_called()
        peer_connectivity._local_network.save_paired_peer.assert_not_called()

    def test_cleartext_http_branch(self) -> None:
        self._assert_held_before_post(
            {"drone_id": "peer-a", "reachable_url": "http://peer.example:8080", "scheme": "http"}
        )

    def test_https_without_a_trust_anchor_branch(self) -> None:
        self._assert_held_before_post(
            {"drone_id": "peer-a", "reachable_url": "https://peer.example:8443", "scheme": "https"}
        )

    def test_tailnet_auto_pair_branch(self) -> None:
        self._assert_held_before_post(
            {"drone_id": "peer-a", "reachable_url": "https://100.64.0.7", "scheme": "https"},
            tailnet_auto_pair=True,
        )

    def test_held_at_entry_never_touches_local_material_or_the_peer(self) -> None:
        """A hold present at entry fails before the certificate manager or peer store is read."""
        _hold()
        manager = self._patch(peer_connectivity, "DroneCertificateManager")
        paired = self._patch(peer_connectivity._local_network, "get_paired_peer")
        urlopen_mock = self._patch(peer_connectivity, "urlopen")
        with self.assertRaises(peer_connectivity.PeerIdentityHold):
            peer_connectivity._local_pair_peer(
                mock.MagicMock(device_id="me"),
                {"drone_id": "peer-a", "reachable_url": "http://peer.example:8080", "scheme": "http"},
                "secret-code",
            )
        manager.assert_not_called()
        paired.assert_not_called()
        urlopen_mock.assert_not_called()


class HeldTailnetSyncTests(_Case):
    def test_held_sync_reports_the_hold_per_device_and_persists_nothing(self) -> None:
        handler = handlers_network.HandlersNetworkMixin()
        handler.settings = mock.MagicMock(use_fake_data=False)
        handler.settings.device_id = "drone-self"
        record = self._patch(handlers_network._local_network, "record_discovered_peer")
        save = self._patch(handlers_network._local_network, "save_paired_peer")
        pair = self._patch(handlers_network, "_local_pair_peer")
        urlopen_mock = self._patch(peer_connectivity, "urlopen")
        _hold()

        rows = handler._sync_tailnet_peers({
            "enrolled": True,
            "peers": [
                {"tailnet_ip": "100.64.0.7", "tailnet_id": "n1", "name": "den"},
                {"tailnet_ip": "100.64.0.8", "tailnet_id": "n2", "name": "attic"},
            ],
        })

        self.assertEqual([row["tailnet_probe_error"] for row in rows], [HOLD_MESSAGE, HOLD_MESSAGE])
        self.assertEqual(json.dumps([row["tailnet_ip"] for row in rows]), '["100.64.0.7", "100.64.0.8"]')
        urlopen_mock.assert_not_called()
        record.assert_not_called()
        save.assert_not_called()
        pair.assert_not_called()


if __name__ == "__main__":
    unittest.main()
