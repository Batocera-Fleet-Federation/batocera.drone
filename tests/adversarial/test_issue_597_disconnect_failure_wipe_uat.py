"""Adversarial UAT for #597: VPN revocation must wipe credentials even when disconnect() raises.

Expected behaviour comes from the issue, not from the implementation. Once the
source peer has confirmed "not sharing" (404) or has left the paired roster,
the imported credentials must be removed and the revocation recorded, whatever
disconnect() does. A failing disconnect is reported, never allowed to abort the
wipe, and never allowed to escape check_sharing_revocation (it runs unattended on
a poller). A transient peer error still revokes nothing and must not even try
to disconnect. The clone-reset path relies on the same wipe.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import app.device.vpn_manager as vpn_manager
from app.device import tailnet_service
from app.drone_api import Settings
from app.transfer import swarm_reset


SAMPLE_OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"
SOURCE_PEER_ID = "source-peer"


def _payload() -> dict:
    return {
        "config_filename": "client.ovpn",
        "config_text": SAMPLE_OVPN,
        "has_credentials": True,
        "username": "peer-user",
        "password": "secret",
    }


def _not_sharing() -> HTTPError:
    return HTTPError("https://peer/v1/api/peer/vpn/config", 404, "Not Found", None, None)


def _revocation_cases():
    # Fresh HTTPError per call: exception instances carry traceback state.
    return (
        ("peer-gone", None, None, vpn_manager._SHARING_REVOKED_PEER_GONE),
        ("peer-off", {"device_id": SOURCE_PEER_ID}, _not_sharing(), vpn_manager._SHARING_REVOKED_PEER_OFF),
    )


@contextlib.contextmanager
def _peer_answers(peer, error=None):
    """The roster lookup returns ``peer``; the peer's vpn/config call raises ``error`` (None = sharing)."""
    with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
            mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer",
                       side_effect=error, return_value={}):
        yield


class _ImportedCredentialsCase(unittest.TestCase):
    def _imported_settings(self) -> Settings:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install")
        install.start()
        self.addCleanup(install.stop)
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-597",
        }, clear=True):
            settings = Settings.from_env()
        vpn_manager.import_from_peer(
            settings, _payload(), source_peer_id=SOURCE_PEER_ID, source_peer_name="Peer One",
        )
        self.assertTrue(vpn_manager.auth_path(settings).exists())
        return settings


class DisconnectFailureWipeUAT(_ImportedCredentialsCase):
    def test_wipe_and_revocation_record_survive_disconnect_error(self) -> None:
        for label, peer, error, reason in _revocation_cases():
            with self.subTest(peer=label):
                settings = self._imported_settings()
                with _peer_answers(peer, error), \
                        mock.patch.object(vpn_manager, "_find_running_openvpn_pid",
                                          side_effect=RuntimeError("process table unreadable")), \
                        contextlib.redirect_stderr(io.StringIO()):
                    vpn_manager.check_sharing_revocation(settings)

                self.assertFalse(vpn_manager.auth_path(settings).exists())
                state = vpn_manager._load_state(settings)
                self.assertFalse(state["has_credentials"])
                self.assertEqual(state["username"], "")
                self.assertEqual(state["revoked_reason"], reason)
                self.assertIsNotNone(state["revoked_at"])
                # Provenance and the orphaned config survive by design (see _revoke_local_credentials).
                self.assertTrue(vpn_manager.config_path(settings).is_file())
                self.assertEqual(state["source_peer_id"], SOURCE_PEER_ID)

    def test_check_never_raises_and_reports_each_disconnect_failure(self) -> None:
        class UnprintableError(Exception):
            def __str__(self) -> str:
                raise RuntimeError("no text for you")

        failures = (
            RuntimeError("process table unreadable"),
            OSError(13, "Permission denied"),
            UnprintableError(),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                settings = self._imported_settings()
                stderr = io.StringIO()
                with _peer_answers(None), \
                        mock.patch.object(vpn_manager, "_find_running_openvpn_pid", side_effect=failure), \
                        contextlib.redirect_stderr(stderr):
                    vpn_manager.check_sharing_revocation(settings)  # must not raise

                self.assertFalse(vpn_manager.auth_path(settings).exists())
                self.assertIn("VPN sharing revocation check failed", stderr.getvalue())

    def test_reports_revocation_as_true_when_wipe_happened(self) -> None:
        # check_sharing_revocation documents "Returns True iff a revocation just happened".
        # The wipe completes before the disconnect exception escapes, so True is the truthful answer.
        for label, peer, error, _reason in _revocation_cases():
            with self.subTest(peer=label):
                settings = self._imported_settings()
                with _peer_answers(peer, error), \
                        mock.patch.object(vpn_manager, "_find_running_openvpn_pid",
                                          side_effect=RuntimeError("process table unreadable")), \
                        contextlib.redirect_stderr(io.StringIO()):
                    revoked = vpn_manager.check_sharing_revocation(settings)

                self.assertFalse(vpn_manager.auth_path(settings).exists())
                self.assertTrue(revoked, "a completed revocation must report True even if disconnect failed")

    def test_disconnect_reporting_error_status_still_wipes(self) -> None:
        # disconnect() does not raise on a kill failure; it returns an error status. The wipe must not depend on it.
        settings = self._imported_settings()
        with _peer_answers(None), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=4242), \
                mock.patch.object(vpn_manager.os, "kill", side_effect=PermissionError(1, "Operation not permitted")), \
                contextlib.redirect_stderr(io.StringIO()):
            revoked = vpn_manager.check_sharing_revocation(settings)

        self.assertTrue(revoked)
        self.assertFalse(vpn_manager.auth_path(settings).exists())
        self.assertFalse(vpn_manager._load_state(settings)["has_credentials"])

    def test_base_exception_from_disconnect_still_wipes_and_propagates(self) -> None:
        # KeyboardInterrupt is a shutdown signal: the wipe must still land, and the signal must not be swallowed.
        settings = self._imported_settings()
        with _peer_answers(None), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                vpn_manager.check_sharing_revocation(settings)

        self.assertFalse(vpn_manager.auth_path(settings).exists())
        self.assertFalse(vpn_manager._load_state(settings)["has_credentials"])

    def test_transient_peer_error_never_disconnects_even_when_disconnect_would_fail(self) -> None:
        transient = (
            HTTPError("https://peer/v1/api/peer/vpn/config", 503, "Unavailable", None, None),
            URLError("unreachable"),
            TimeoutError("timed out"),
        )
        for error in transient:
            with self.subTest(error=type(error).__name__):
                settings = self._imported_settings()
                disconnect = mock.Mock(side_effect=RuntimeError("must not be reached"))
                with _peer_answers({"device_id": SOURCE_PEER_ID}, error), \
                        mock.patch.object(vpn_manager, "disconnect", disconnect):
                    self.assertFalse(vpn_manager.check_sharing_revocation(settings))

                disconnect.assert_not_called()
                self.assertTrue(vpn_manager.auth_path(settings).exists())
                self.assertTrue(vpn_manager._load_state(settings)["has_credentials"])

    def test_next_tick_after_failed_revocation_is_quiet(self) -> None:
        settings = self._imported_settings()
        with _peer_answers(None), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid",
                                  side_effect=RuntimeError("process table unreadable")), \
                contextlib.redirect_stderr(io.StringIO()):
            vpn_manager.check_sharing_revocation(settings)
        before = vpn_manager._load_state(settings)

        with _peer_answers(None), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))

        self.assertEqual(stderr.getvalue(), "")
        after = vpn_manager._load_state(settings)
        self.assertEqual(after["revoked_reason"], before["revoked_reason"])
        self.assertEqual(after["revoked_at"], before["revoked_at"])

    def test_clone_reset_wipes_imported_credentials_when_disconnect_raises(self) -> None:
        # Clone provisioning reaches the same wipe via swarm_reset; the failing vpn step must be reported
        # as incomplete while the credentials are still removed.
        settings = self._imported_settings()
        with mock.patch.object(vpn_manager, "_find_running_openvpn_pid",
                               side_effect=RuntimeError("process table unreadable")), \
                mock.patch.object(tailnet_service, "_revoke_local_sharing"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            incomplete = swarm_reset._revoke_shared_credentials(settings)

        self.assertIn("vpn", incomplete)
        self.assertFalse(vpn_manager.auth_path(settings).exists())
        state = vpn_manager._load_state(settings)
        self.assertFalse(state["has_credentials"])
        self.assertEqual(state["revoked_reason"], swarm_reset.CLONE_REVOCATION_REASON)


if __name__ == "__main__":
    unittest.main()
