"""Issue #411: cleartext-http peer calls honour the identity hold.

``_drone_client_ssl_context`` used to return early for non-https URLs, before consulting
``swarm_networking_allowed()``, so a held (RECOVERY_REQUIRED) Drone still sent http://
peer GET/POST/download requests. The hold is now checked first: every outbound peer call,
whatever the scheme, raises ``PeerIdentityHold`` without touching the network.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning
from app.common.settings import Settings
from app.transfer import peer_connectivity


def _response() -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({"ok": True}).encode("utf-8")
    return response


class HttpPeerCallsHonourIdentityHoldTests(unittest.TestCase):
    def setUp(self) -> None:
        identity_provisioning.reset_runtime_state_for_tests()
        self.addCleanup(identity_provisioning.reset_runtime_state_for_tests)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        env = {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"), "DRONE_STATE_DATABASE_FILE": str(root / "state.db"),
            "DRONE_DEVICE_ID": "drone-self",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        identity_provisioning._publish(
            identity_provisioning.State.RECOVERY_REQUIRED, "held", "adversarial hold", False
        )

    def test_held_http_peer_get_is_refused(self) -> None:
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response()) as urlopen_mock:
            with self.assertRaises(peer_connectivity.PeerIdentityHold):
                peer_connectivity._peer_get_json("http://10.0.0.5:8080/health", self.settings)
        urlopen_mock.assert_not_called()

    def test_held_http_peer_post_is_refused(self) -> None:
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response()) as urlopen_mock:
            with self.assertRaises(peer_connectivity.PeerIdentityHold):
                peer_connectivity._peer_post_json("http://10.0.0.5:8080/x", {"a": 1}, self.settings)
        urlopen_mock.assert_not_called()

    def test_held_http_peer_route_failover_helpers_raise_hold(self) -> None:
        peer = {"drone_id": "drone-peer", "addresses": ["http://10.0.0.5:8080", "http://10.0.0.6:8080"]}
        candidates = ["http://10.0.0.5:8080", "http://10.0.0.6:8080"]
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response()) as urlopen_mock, \
                mock.patch.object(peer_connectivity, "_peer_address_candidates", return_value=candidates), \
                mock.patch.object(peer_connectivity, "_candidate_dialable", return_value=True):
            with self.assertRaises(peer_connectivity.PeerIdentityHold):
                peer_connectivity._peer_get_json_for_peer(peer, "/health", self.settings)
            with self.assertRaises(peer_connectivity.PeerIdentityHold):
                peer_connectivity._peer_post_json_for_peer(peer, "/x", {"a": 1}, self.settings)
            destination = Path(tempfile.mkdtemp()) / "out.bin"
            self.addCleanup(lambda: __import__("shutil").rmtree(destination.parent, ignore_errors=True))
            with self.assertRaises(peer_connectivity.PeerIdentityHold):
                peer_connectivity._peer_download_file_for_peer(peer, "/f", destination, self.settings)
        urlopen_mock.assert_not_called()
        self.assertFalse(destination.exists())

    def test_held_http_peer_download_helpers_share_the_gated_context(self) -> None:
        # peer_download builds its urlopen context through the same helper, so an http://
        # direct download is refused before any request is opened while the identity is held.
        from app.transfer import peer_download

        self.assertIs(peer_download._drone_client_ssl_context, peer_connectivity._drone_client_ssl_context)
        with self.assertRaises(peer_connectivity.PeerIdentityHold):
            peer_download._drone_client_ssl_context(self.settings, "http://10.0.0.5:8080/v1/api/peer/roms/nes/a.zip")

    def test_http_peer_calls_still_work_when_not_held(self) -> None:
        identity_provisioning.reset_runtime_state_for_tests()
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response()) as urlopen_mock:
            self.assertEqual(peer_connectivity._peer_get_json("http://10.0.0.5:8080/health", self.settings), {"ok": True})
            self.assertEqual(
                peer_connectivity._peer_post_json("http://10.0.0.5:8080/x", {"a": 1}, self.settings), {"ok": True}
            )
        self.assertEqual(urlopen_mock.call_count, 2)

    def test_held_https_peer_get_is_refused_for_contrast(self) -> None:
        with mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock:
            with self.assertRaises(peer_connectivity.PeerIdentityHold):
                peer_connectivity._peer_get_json("https://10.0.0.5:8443/health", self.settings)
        urlopen_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
