"""Out-of-scope finding found while testing #267: cleartext-http peer calls ignore the hold.

Issue #267 gates the pair-by-address peer-info fetch and the pairing TLS probe. The shared
outbound helpers ``_peer_get_json`` / ``_peer_post_json`` (and the worker download helper)
reach the network through ``_drone_client_ssl_context``, whose identity check sits *after*
its ``scheme != "https"`` early return. For an ``http://`` peer address the hold is
therefore never consulted and the request is sent while ``swarm_networking_allowed()`` is
False. CLAUDE.md says outbound peer traffic is gated while the identity is held.

These assertions pin the observed behaviour as evidence so the worker can file it
separately; they are not a blocking requirement of #267.
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


class HttpPeerCallsIgnoreIdentityHoldTests(unittest.TestCase):
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

    def test_held_http_peer_get_is_still_sent(self) -> None:
        """Observed: GET to an http:// peer is made while held (an https:// one raises the hold)."""
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response()) as urlopen_mock:
            result = peer_connectivity._peer_get_json("http://10.0.0.5:8080/health", self.settings)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen_mock.call_count, 1)

    def test_held_http_peer_post_is_still_sent(self) -> None:
        with mock.patch.object(peer_connectivity, "urlopen", return_value=_response()) as urlopen_mock:
            result = peer_connectivity._peer_post_json("http://10.0.0.5:8080/x", {"a": 1}, self.settings)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen_mock.call_count, 1)

    def test_held_https_peer_get_is_refused_for_contrast(self) -> None:
        with mock.patch.object(peer_connectivity, "urlopen") as urlopen_mock:
            with self.assertRaises(peer_connectivity.PeerIdentityHold):
                peer_connectivity._peer_get_json("https://10.0.0.5:8443/health", self.settings)
        urlopen_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
