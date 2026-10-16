"""Issue #267 adversarial boundaries: a held identity dials nobody during pair-by-address.

Expected, derived from the issue and CLAUDE.md before reading the patch:

* While swarm networking is held, no outbound peer request is made for any address
  form (bare host, IPv6, either scheme, default port), and the admin reply is the local
  503 hold text, never the 502 "could not reach" text.
* A hold that lands between the route-entry check and a live dial (while a TLS context is
  being built, or during the first TLS probe) stops the next dial, including the pairing
  POST after a probe that matched the pinned fingerprint.
* The hold is state, not a sticky label: once resolved, dials and the 502 mapping return,
  and a PermissionError with an errno is a real OS failure rather than a hold.
* The tailnet scan reuses the peer-info fetch, so a held Drone probes no tailnet device.

State is set through the real ``identity_provisioning`` gate; only network primitives and
local stores are replaced.
"""

from __future__ import annotations

import errno
import json
import unittest
from unittest import mock
from urllib.error import URLError

from app.common import identity_provisioning
from app.transfer import peer_connectivity
from app.web import handlers_network

FINGERPRINT = "ab" * 32
HOLD_MESSAGE = "peer pairing is held pending Drone identity verification"
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


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        identity_provisioning.reset_runtime_state_for_tests()
        self.addCleanup(identity_provisioning.reset_runtime_state_for_tests)

    def _patch(self, target, attribute: str, **kwargs):
        patcher = mock.patch.object(target, attribute, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started


class HeldAddressFormsTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler.settings.device_id = "drone-self"
        self.handler._send_json = mock.Mock()
        self._patch(handlers_network._local_network, "is_local_mode", return_value=True)
        self.save = self._patch(handlers_network._local_network, "save_paired_peer")

    def test_every_address_form_gets_the_local_503_and_no_dial(self) -> None:
        _hold()
        for address in (
            "peer.example", "peer.example:8443", "100.64.0.9", "[fd7a:115c::1]:8443",
            "https://peer.example", "http://peer.example:80", "https://PEER.example:443/",
        ):
            with self.subTest(address=address), \
                    mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock, \
                    mock.patch.object(peer_connectivity.socket, "create_connection") as connect_mock, \
                    mock.patch.object(peer_connectivity.subprocess, "run") as run_mock:
                self.handler._send_json.reset_mock()
                self.handler._handle_admin_local_peer_pair_by_address(
                    {"address": address, "pairing_code": "secret", "certificate_fingerprint": FINGERPRINT}
                )
                self.assertEqual(self.handler._send_json.call_args.args, (503, {"error": HOLD_MESSAGE}))
                urlopen_mock.assert_not_called()
                connect_mock.assert_not_called()
                run_mock.assert_not_called()
        self.save.assert_not_called()

    def test_hold_message_does_not_echo_the_dialed_address(self) -> None:
        _hold()
        self.handler._handle_admin_local_peer_pair_by_address({"address": "https://secret-host.example:9"})
        _, payload = self.handler._send_json.call_args.args
        self.assertNotIn("secret-host", json.dumps(payload))

    def test_resolved_identity_dials_again_and_unreachable_is_502(self) -> None:
        _hold()
        with mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock:
            self.handler._handle_admin_local_peer_pair_by_address({"address": "https://peer.example:8443"})
        urlopen_mock.assert_not_called()

        identity_provisioning.reset_runtime_state_for_tests()  # back to the unheld default
        self.handler._send_json.reset_mock()
        with mock.patch.object(peer_connectivity, "urlopen", side_effect=URLError("refused")) as urlopen_mock:
            self.handler._handle_admin_local_peer_pair_by_address({"address": "https://peer.example:8443"})
        self.assertEqual(urlopen_mock.call_count, 1)
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("could not reach a Drone at", payload["error"])

    def test_errno_permission_error_from_the_fetch_is_not_a_hold(self) -> None:
        with mock.patch.object(peer_connectivity, "urlopen", side_effect=PermissionError(errno.EACCES, "denied")):
            self.handler._handle_admin_local_peer_pair_by_address({"address": "https://peer.example:8443"})
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertNotIn(HOLD_MESSAGE, payload["error"])

    def test_bad_address_never_dials_even_when_held(self) -> None:
        _hold()
        with mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock:
            for address in ("ftp://peer.example", "https://", "http://host:notaport"):
                with self.subTest(address=address):
                    try:
                        self.handler._handle_admin_local_peer_pair_by_address({"address": address})
                    except ValueError:
                        pass  # the route layer maps a malformed address to 400
        urlopen_mock.assert_not_called()


class MidFlightHoldTests(_Case):
    def test_hold_landing_while_the_https_context_is_built_stops_the_peer_info_get(self) -> None:
        def context_then_hold():
            _hold()
            return mock.MagicMock()

        with mock.patch.object(peer_connectivity.ssl, "_create_unverified_context", side_effect=context_then_hold), \
                mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock:
            with self.assertRaisesRegex(peer_connectivity.PeerIdentityHold, HOLD_MESSAGE):
                peer_connectivity._fetch_peer_info("https://peer.example:8443")
        urlopen_mock.assert_not_called()

    def test_hold_landing_while_the_probe_context_is_built_stops_the_tls_probe(self) -> None:
        def context_then_hold():
            _hold()
            return mock.MagicMock()

        with mock.patch.object(peer_connectivity.ssl, "_create_unverified_context", side_effect=context_then_hold), \
                mock.patch.object(peer_connectivity.socket, "create_connection") as connect_mock:
            with self.assertRaisesRegex(peer_connectivity.PeerIdentityHold, HOLD_MESSAGE):
                peer_connectivity._pairing_tls_context("https://peer.example:8443", FINGERPRINT)
        connect_mock.assert_not_called()

    def test_hold_landing_during_a_matching_probe_stops_the_pairing_post(self) -> None:
        """The probe proved the pinned leaf, then the hold landed: the code must not be sent."""
        self._patch(peer_connectivity._local_network, "is_local_mode", return_value=True)
        self._patch(peer_connectivity._local_network, "get_paired_peer", return_value={})
        self._patch(peer_connectivity._local_network, "discovery_payload", return_value={})
        self._patch(peer_connectivity, "DroneCertificateManager")
        self._patch(peer_connectivity, "_drone_scheme", return_value="https")
        self._patch(peer_connectivity, "_drone_advertised_api_port", return_value=8443)
        self._patch(peer_connectivity, "_drone_advertised_peer_mtls_port", return_value=8443)
        urlopen_mock = self._patch(peer_connectivity, "urlopen")
        pinned_context = mock.MagicMock()
        self._patch(peer_connectivity, "_pinned_pairing_context", return_value=pinned_context)

        def leaf_matching_pin(*_args, **_kwargs) -> bytes:
            _hold()
            return b"der"

        tls = mock.MagicMock()
        tls.__enter__.return_value.getpeercert.side_effect = leaf_matching_pin
        probe = mock.MagicMock()
        probe.wrap_socket.return_value = tls
        self._patch(peer_connectivity.ssl, "_create_unverified_context", return_value=probe)
        self._patch(peer_connectivity.socket, "create_connection")
        self._patch(peer_connectivity.hashlib, "sha256").return_value.hexdigest.return_value = FINGERPRINT

        peer = {"drone_id": "peer-a", "reachable_url": "https://peer.example:8443", "scheme": "https",
                "api_port": 8443}
        with self.assertRaises(peer_connectivity.PeerIdentityHold):
            peer_connectivity._local_pair_peer(
                mock.MagicMock(device_id="me"), peer, "secret-code", confirmed_fingerprint=FINGERPRINT
            )
        urlopen_mock.assert_not_called()


class TailnetProbeHoldTests(_Case):
    def test_held_tailnet_probe_dials_nothing_and_reports_the_hold(self) -> None:
        _hold()
        with mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock:
            device, info, error = handlers_network.HandlersNetworkMixin._probe_tailnet_device(
                {"tailnet_ip": "100.64.0.7"}
            )
        urlopen_mock.assert_not_called()
        self.assertIsNone(info)
        self.assertEqual(error, HOLD_MESSAGE)

    def test_unheld_tailnet_probe_still_reaches_the_peer(self) -> None:
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response(PEER_INFO)) as urlopen_mock:
            _, info, error = handlers_network.HandlersNetworkMixin._probe_tailnet_device(
                {"tailnet_ip": "100.64.0.7"}
            )
        self.assertEqual(urlopen_mock.call_count, 1)
        self.assertEqual(error, "")
        self.assertEqual(info["drone_id"], "peer-a")


if __name__ == "__main__":
    unittest.main()
