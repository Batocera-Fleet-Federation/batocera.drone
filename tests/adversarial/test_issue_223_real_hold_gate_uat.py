"""Issue #223 adversarial UAT: the identity hold is exercised through the real gate.

Expected, derived from the issue before reading the patch:

* While this Drone's identity is unresolved, a pair-by-address or discovered-peer
  pairing is refused by the local hold with HTTP 503 and the hold's own text. It
  must never read as the "Could not reach that drone" 502.
* The refusal happens before any pairing POST leaves the Drone, and before any
  paired-peer row is saved.
* A hold that lands while the peer-info fetch is in flight still stops the POST.
* The 503 is a state, not a sticky label: once the hold lifts, a real connection
  failure is reported as the 502 it is again.
* 502 stays the answer for a connection failure. An errno-less OSError (or an
  SSL error) is not a hold, and a PermissionError that is not the hold while
  identity is resolved is not a hold either.

These tests use the real ``swarm_networking_allowed`` gate (state is set through
``identity_provisioning``), not a mock of it. Only the network, the certificate
manager and the local peer store are replaced.
"""

from __future__ import annotations

import errno
import json
import ssl
import unittest
from unittest import mock
from urllib.error import URLError

from app.common import identity_provisioning
from app.transfer import peer_connectivity
from app.web import handlers_network

FINGERPRINT = "ab" * 32
HOLD_MESSAGE = "peer pairing is held pending Drone identity verification"
HTTPS_PEER = "https://peer.example:8443"
HTTP_PEER = "http://peer.example:8080"
PEER_INFO = {
    "service": peer_connectivity._local_network.DISCOVERY_SERVICE,
    "drone_id": "peer-a",
    "name": "peer-a",
    "api_port": 8443,
    "certificate_fingerprint": FINGERPRINT,
}


def _hold() -> None:
    identity_provisioning._publish(
        identity_provisioning.State.RECOVERY_REQUIRED, "held", "adversarial hold", False
    )


def _response(payload: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return response


class _PairingCase(unittest.TestCase):
    def setUp(self) -> None:
        identity_provisioning.reset_runtime_state_for_tests()
        self.addCleanup(identity_provisioning.reset_runtime_state_for_tests)
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler.settings.device_id = "drone-self"
        self.handler._send_json = mock.Mock()
        self.posts: list[str] = []
        self.save = self._patch(handlers_network._local_network, "save_paired_peer")
        self._patch(handlers_network._local_network, "is_local_mode", return_value=True)
        self._patch(handlers_network._local_network, "get_paired_peer", return_value={})
        self._patch(handlers_network._local_network, "discovered_peers", return_value=[])
        self._patch(peer_connectivity._local_network, "discovery_payload", return_value={})
        self._patch(peer_connectivity, "DroneCertificateManager")
        self._patch(peer_connectivity, "_drone_scheme", return_value="http")
        self._patch(peer_connectivity, "_drone_advertised_api_port", return_value=8080)
        self._patch(peer_connectivity, "_drone_advertised_peer_mtls_port", return_value=8080)
        self.urlopen = self._patch(peer_connectivity, "urlopen", side_effect=self._network)
        self.peer_info_reply = _response(PEER_INFO)
        self.pair_error: Exception | None = None

    def _patch(self, target, attribute: str, **kwargs):
        patcher = mock.patch.object(target, attribute, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def _network(self, request, *args, **kwargs):
        url = request.full_url
        if url.endswith("/v1/api/peer/info"):
            return self.peer_info_reply
        self.posts.append(url)
        if self.pair_error is not None:
            raise self.pair_error
        return _response({"status": "paired", "drone_id": "peer-a"})

    def _last_reply(self) -> tuple:
        return self.handler._send_json.call_args.args


class HeldPairByAddressTests(_PairingCase):
    def test_held_https_pair_by_address_is_503_and_never_posts_the_code(self) -> None:
        _hold()
        self.handler._handle_admin_local_peer_pair_by_address(
            {"address": HTTPS_PEER, "pairing_code": "not-a-code", "certificate_fingerprint": FINGERPRINT}
        )

        self.assertEqual(self.handler._send_json.call_count, 1)
        self.assertEqual(self._last_reply(), (503, {"error": HOLD_MESSAGE}))
        self.assertEqual(self.posts, [])
        self.save.assert_not_called()

    def test_held_http_pair_by_address_is_503_and_never_posts_the_code(self) -> None:
        _hold()
        self.handler._handle_admin_local_peer_pair_by_address(
            {"address": HTTP_PEER, "pairing_code": "not-a-code", "certificate_fingerprint": FINGERPRINT}
        )

        self.assertEqual(self._last_reply(), (503, {"error": HOLD_MESSAGE}))
        self.assertEqual(self.posts, [])
        self.save.assert_not_called()

    def test_hold_landing_while_the_peer_info_fetch_is_in_flight_stops_the_post(self) -> None:
        """Gate one passes at route entry; the hold lands during GET /peer/info."""

        def fetch_then_hold(request, *args, **kwargs):
            if request.full_url.endswith("/v1/api/peer/info"):
                _hold()
                return self.peer_info_reply
            return self._network(request, *args, **kwargs)

        self.urlopen.side_effect = fetch_then_hold
        self.handler._handle_admin_local_peer_pair_by_address(
            {"address": HTTP_PEER, "pairing_code": "not-a-code", "certificate_fingerprint": FINGERPRINT}
        )

        self.assertEqual(self._last_reply(), (503, {"error": HOLD_MESSAGE}))
        self.assertEqual(self.posts, [])
        self.save.assert_not_called()


class HeldDiscoveredPeerTests(_PairingCase):
    def test_held_discovered_peer_with_pinned_fingerprint_is_503_without_a_code(self) -> None:
        """The discovered route makes no peer-info fetch; a pinned peer needs no code."""
        peer = {"drone_id": "peer-a", "reachable_url": HTTPS_PEER}
        self._patch(handlers_network._local_network, "get_paired_peer", return_value={"certificate_fingerprint": FINGERPRINT})
        _hold()
        self.assertIsNone(self.handler._pair_peer_or_reply(peer, "", ""))

        self.assertEqual(self._last_reply(), (503, {"error": HOLD_MESSAGE}))
        self.urlopen.assert_not_called()
        self.save.assert_not_called()

    def test_held_discovered_peer_route_is_503_not_unreachable(self) -> None:
        peer = {"drone_id": "peer-a", "reachable_url": HTTPS_PEER}
        self._patch(handlers_network._local_network, "discovered_peers", return_value=[peer])
        self._patch(handlers_network._local_network, "get_paired_peer", return_value={"certificate_fingerprint": FINGERPRINT})
        _hold()
        self.handler._handle_admin_local_peer_pair("peer-a", {"pairing_code": "", "certificate_fingerprint": ""})

        self.assertEqual(self._last_reply(), (503, {"error": HOLD_MESSAGE}))
        self.assertNotIn("reach", self._last_reply()[1]["error"].lower())
        self.assertEqual(self.posts, [])


class HoldLiftsTests(_PairingCase):
    def test_hold_is_not_sticky_once_identity_is_resolved_again(self) -> None:
        peer = {"drone_id": "peer-a", "reachable_url": HTTP_PEER}
        _hold()
        self.assertIsNone(self.handler._pair_peer_or_reply(peer, "x", "ab" * 32))
        self.assertEqual(self._last_reply()[0], 503)

        identity_provisioning.reset_runtime_state_for_tests()
        self.pair_error = URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))
        self.assertIsNone(self.handler._pair_peer_or_reply(peer, "x", "ab" * 32))

        status, payload = self._last_reply()
        self.assertEqual(status, 502)
        self.assertIn("Could not reach that drone", payload["error"])
        self.assertEqual(len(self.posts), 1)


class ConnectionFailureBoundaryTests(_PairingCase):
    """With identity resolved, nothing here may be reported as the local hold."""

    def setUp(self) -> None:
        super().setUp()
        self.peer = {"drone_id": "peer-a", "reachable_url": HTTP_PEER}

    def test_os_level_permission_errors_stay_connection_failures(self) -> None:
        for code in (errno.EACCES, errno.EPERM):
            with self.subTest(errno=code):
                self.handler._send_json.reset_mock()
                self.pair_error = PermissionError(code, "Permission denied")
                self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", "ab" * 32))
                status, payload = self._last_reply()
                self.assertEqual(status, 502)
                self.assertIn("Could not reach that drone", payload["error"])

    def test_errno_less_os_error_is_not_widened_into_a_hold(self) -> None:
        self.pair_error = OSError("network is down")
        self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", "ab" * 32))

        status, payload = self._last_reply()
        self.assertEqual(status, 502)
        self.assertIn("Could not reach that drone", payload["error"])

    def test_ssl_failure_is_a_connection_failure_not_a_hold(self) -> None:
        self.pair_error = ssl.SSLError("certificate verify failed")
        self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", "ab" * 32))

        status, _ = self._last_reply()
        self.assertEqual(status, 502)

    def test_errno_less_permission_error_while_resolved_is_not_reported_as_a_hold(self) -> None:
        """A bare PermissionError is only the hold when the hold is active.

        The mapping must follow the hold state, not just the shape of the exception.
        While identity is resolved, a bare PermissionError is an ordinary failure:
        it must not be answered 503 as if this Drone's own state refused it.
        """
        self.pair_error = PermissionError("unexpected denial from the network stack")
        self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", "ab" * 32))

        status, payload = self._last_reply()
        self.assertEqual(status, 502)
        self.assertIn("Could not reach that drone", payload["error"])


class HoldFlipDuringPairingTests(_PairingCase):
    def test_flip_during_peer_info_with_pinned_https_peer_never_reaches_the_post(self) -> None:
        """A pinned https peer: the hold lands during the peer-info fetch of pair-by-address."""
        self._patch(handlers_network._local_network, "get_paired_peer", return_value={"certificate_fingerprint": FINGERPRINT})

        def fetch_then_hold(request, *args, **kwargs):
            if request.full_url.endswith("/v1/api/peer/info"):
                _hold()
                return self.peer_info_reply
            return self._network(request, *args, **kwargs)

        self.urlopen.side_effect = fetch_then_hold
        self.handler._handle_admin_local_peer_pair_by_address({"address": HTTPS_PEER, "pairing_code": ""})

        self.assertEqual(self._last_reply(), (503, {"error": HOLD_MESSAGE}))
        self.assertEqual(self.posts, [])
        self.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
