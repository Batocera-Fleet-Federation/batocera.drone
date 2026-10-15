"""Adversarial UAT for issue #266: local EACCES during pairing setup is not "Could not reach that drone".

Expectations come from the issue title and the local-versus-peer distinction, not from the diff:

- A local OS failure on this Drone's own side while setting up a pair (its own certificate/key,
  the pinned peer-certificate cache written after the peer answers, or any other local step before
  the peer is contacted) must not be reported as an unreachable peer (502 "Could not reach that drone").
- The local reply may name the OS reason ("Permission denied") but never a filesystem path.
- Genuine network failures and peer-side answers keep their existing 502 mapping.
- The identity-provisioning hold keeps its own 503 and is not turned into a local fault.
"""

import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from app.common.settings import Settings
from app.transfer import peer_connectivity
from app.web import handlers_network

PEER = {"drone_id": "peer-a", "reachable_url": "http://10.0.0.5:8080"}
PAIRING_CODE = "12345678"
UNREACHABLE = "Could not reach that drone"


class _PairingSetupCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        env = {
            "USERDATA_ROOT": str(self.root / "userdata"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.db"),
            "DRONE_DEVICE_ID": "drone-self",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = self.settings
        self.handler._send_json = mock.Mock()
        self._patch(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True)
        self._patch(peer_connectivity._local_network, "is_local_mode", return_value=True)
        self.certificate_manager = self._patch(peer_connectivity, "DroneCertificateManager")
        self.certificate_manager.return_value.ensure_certificate.return_value = {
            "public_certificate": "-----BEGIN CERTIFICATE-----own",
            "fingerprint": "cc" * 32,
        }
        self.urlopen = self._patch(peer_connectivity, "urlopen", side_effect=self._accepting_response)

    def _patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    @staticmethod
    def _accepting_response(*args, **kwargs):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"status": "paired", "drone_id": "peer-a", "certificate_pem": "-----BEGIN CERTIFICATE-----peer"}
        ).encode("utf-8")
        return response

    def _reply(self):
        """Return (status, payload) from the single pairing reply the handler sent."""
        self.assertEqual(self.handler._send_json.call_count, 1, "exactly one reply expected")
        return self.handler._send_json.call_args.args


class LocalCertificateWriteAfterPeerAcceptsTests(_PairingSetupCase):
    def test_pinned_cache_write_eacces_is_not_reported_as_unreachable(self) -> None:
        # The peer has already answered and accepted the pair; the failure is ours.
        self._patch(peer_connectivity, "_certificate_pem_fingerprint", return_value="ee" * 32)
        cache_dir = peer_connectivity._local_peer_cert_cache_path(self.settings, "peer-a").parent
        cache_dir.mkdir(parents=True)
        cache_dir.chmod(0o500)
        self.addCleanup(cache_dir.chmod, 0o700)

        result = self.handler._pair_peer_or_reply(PEER, PAIRING_CODE, "")

        self.assertIsNone(result)
        status, payload = self._reply()
        self.assertNotEqual(status, 502, "a local write failure is not an upstream 502")
        self.assertNotIn(UNREACHABLE, payload["error"])
        self.assertIn("Permission denied", payload["error"])
        self.assertNotIn(str(self.root), payload["error"], "the reply must not leak a filesystem path")

    def test_pinned_cache_write_error_message_never_names_the_path(self) -> None:
        cache_path = peer_connectivity._local_peer_cert_cache_path(self.settings, "peer-a")
        denied = PermissionError(errno.EACCES, "Permission denied", str(cache_path))
        self._patch(peer_connectivity, "_save_local_peer_certificate", side_effect=denied)

        self.assertIsNone(self.handler._pair_peer_or_reply(PEER, PAIRING_CODE, ""))

        status, payload = self._reply()
        self.assertNotIn(UNREACHABLE, payload["error"])
        self.assertNotIn("peer-a.crt", payload["error"])
        self.assertNotIn("/userdata", payload["error"])
        self.assertNotIn(str(cache_path), payload["error"])


class LocalFailureBeforeContactingPeerTests(_PairingSetupCase):
    def test_certificate_access_errors_are_local_faults_not_unreachable(self) -> None:
        # Read-only root filesystems (Batocera) surface EROFS, not only EACCES.
        for code in (errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOSPC):
            with self.subTest(errno=errno.errorcode[code]):
                self.handler._send_json.reset_mock()
                self.certificate_manager.return_value.ensure_certificate.side_effect = OSError(
                    code, os.strerror(code), str(self.root / "drone.key")
                )

                self.assertIsNone(self.handler._pair_peer_or_reply(PEER, PAIRING_CODE, ""))

                status, payload = self._reply()
                self.assertEqual(status, 500)
                self.assertNotIn(UNREACHABLE, payload["error"])
                self.assertNotIn("drone.key", payload["error"])
                self.assertNotIn(str(self.root), payload["error"])
                self.urlopen.assert_not_called()

    def test_errno_less_permission_error_from_certificate_setup_is_not_unreachable(self) -> None:
        self.certificate_manager.return_value.ensure_certificate.side_effect = PermissionError("denied")

        self.assertIsNone(self.handler._pair_peer_or_reply(PEER, PAIRING_CODE, ""))

        status, payload = self._reply()
        self.assertEqual(status, 500)
        self.assertNotIn(UNREACHABLE, payload["error"])
        self.urlopen.assert_not_called()

    def test_other_local_permission_error_during_setup_is_not_unreachable(self) -> None:
        # Any local EACCES while preparing the pair, not only the certificate step, happens
        # before the peer is contacted, so calling the peer unreachable is factually wrong.
        denied = PermissionError(errno.EACCES, "Permission denied", str(self.root / "identity" / "binding.json"))
        self._patch(peer_connectivity._local_network, "discovery_payload", side_effect=denied)

        self.assertIsNone(self.handler._pair_peer_or_reply(PEER, PAIRING_CODE, ""))

        status, payload = self._reply()
        self.assertNotIn(UNREACHABLE, payload["error"])
        self.assertNotIn("binding.json", payload["error"])
        self.urlopen.assert_not_called()


class ClassificationGuardTests(_PairingSetupCase):
    def test_genuine_network_failure_is_still_unreachable(self) -> None:
        self.urlopen.side_effect = URLError("timed out")

        self.assertIsNone(self.handler._pair_peer_or_reply(PEER, PAIRING_CODE, ""))

        status, payload = self._reply()
        self.assertEqual(status, 502)
        self.assertIn(UNREACHABLE, payload["error"])

    def test_identity_hold_stays_503_even_when_certificate_access_would_fail(self) -> None:
        self._patch(peer_connectivity, "_identity_swarm_networking_allowed", return_value=False)
        self.certificate_manager.return_value.ensure_certificate.side_effect = PermissionError(
            errno.EACCES, "Permission denied", str(self.root / "drone.key")
        )

        self.assertIsNone(self.handler._pair_peer_or_reply(PEER, PAIRING_CODE, ""))

        status, payload = self._reply()
        self.assertEqual(status, 503)
        self.assertNotIn(UNREACHABLE, payload["error"])
        self.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
