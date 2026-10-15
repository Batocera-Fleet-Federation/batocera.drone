"""Issue #266: local certificate setup failures are not peer reachability errors."""

from __future__ import annotations

import errno
import tempfile
import unittest
from unittest import mock

from app.common.settings import Settings
from app.transfer import peer_connectivity
from app.web import handlers_network


FINGERPRINT = "ab" * 32


class LocalPairingCertificateErrors(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = self.temp.name
        env = {
            "USERDATA_ROOT": root,
            "ROMS_ROOT": f"{root}/roms",
            "BIOS_ROOT": f"{root}/bios",
            "SAVES_ROOT": f"{root}/saves",
            "DRONE_STATE_DATABASE_FILE": f"{root}/state.db",
            "DRONE_DEVICE_ID": "drone-self",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = self.settings
        self.handler._send_json = mock.Mock()
        self.peer = {"drone_id": "peer-a", "reachable_url": "https://peer.example"}

    def _pair_with_certificate_error(self, error):
        patches = (
            mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True),
            mock.patch.object(peer_connectivity._local_network, "is_local_mode", return_value=True),
            mock.patch.object(peer_connectivity.DroneCertificateManager, "ensure_certificate", side_effect=error),
        )
        with patches[0], patches[1], patches[2]:
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "secret-code", FINGERPRINT))
        status, payload = self.handler._send_json.call_args.args
        return status, payload["error"]

    def test_eacces_during_local_key_setup_is_500_and_does_not_disclose_path(self):
        private_path = f"{self.temp.name}/secrets/drone-private.key"
        status, message = self._pair_with_certificate_error(
            PermissionError(errno.EACCES, "Permission denied", private_path)
        )

        self.assertEqual(status, 500)
        self.assertIn("Permission denied", message)
        self.assertNotIn(private_path, message)
        self.assertNotIn("Could not reach that drone", message)
        self.assertNotIn("secret-code", message)

    def test_os_error_without_strerror_uses_safe_type_name(self):
        private_path = f"{self.temp.name}/missing/drone.crt"
        failure = OSError(errno.EIO, "", private_path)
        status, message = self._pair_with_certificate_error(failure)

        self.assertEqual(status, 500)
        self.assertIn("OSError", message)
        self.assertNotIn(private_path, message)

    def test_identity_hold_is_still_service_unavailable(self):
        hold = handlers_network.PeerIdentityHold("identity verification pending")
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=hold):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))

        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], str(hold))


if __name__ == "__main__":
    unittest.main()
