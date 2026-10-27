"""UAT for #597: a failing disconnect() must not abort the credential wipe.

Expected behavior, derived from the issue before reading the patch: when a
source peer no longer shares (404) or is no longer paired, revocation must
remove the imported ``auth.txt``, clear ``has_credentials``/``username`` and
record ``revoked_reason``/``revoked_at`` even if ``disconnect()`` raises. The
disconnect failure must not be silently swallowed inside the revocation helper
(the original error still surfaces to its caller), and the unattended check
must never raise. Once revoked, later ticks must not retry or re-raise on the
same already-wiped state. A BaseException raised by disconnect() (a shutdown
signal) is not swallowed either, but the wipe still happens first.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings
from urllib.error import HTTPError


SAMPLE_OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"
PEER = {"drone_id": "peer-1", "name": "Peer One"}


def _not_found() -> HTTPError:
    return HTTPError("https://peer/v1/api/peer/vpn/config", 404, "not found", None, None)


class Issue597DisconnectFinallyUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install")
        self.addCleanup(install.stop)
        install.start()
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-597-disconnect-finally",
        }, clear=True):
            self.settings = Settings.from_env()
        vpn_manager.import_from_peer(
            self.settings,
            {
                "config_filename": "client.ovpn", "config_text": SAMPLE_OVPN,
                "has_credentials": True, "username": "peeruser", "password": "peerpass123",
            },
            source_peer_id="peer-1", source_peer_name="Peer One",
        )
        self.assertTrue(vpn_manager.auth_path(self.settings).exists(), "precondition: imported credentials on disk")

    def _assert_wiped(self, reason: str) -> None:
        self.assertFalse(vpn_manager.auth_path(self.settings).exists())
        state = vpn_manager._load_state(self.settings)
        self.assertFalse(state["has_credentials"])
        self.assertEqual(state["username"], "")
        self.assertEqual(state["revoked_reason"], reason)
        self.assertIsNotNone(state["revoked_at"])
        self.assertTrue(vpn_manager.config_path(self.settings).is_file())
        self.assertEqual(state["source_peer_id"], "peer-1")

    def test_real_disconnect_failure_still_wipes_on_peer_off(self) -> None:
        # Reproduction from the issue: the real disconnect() path, failing inside
        # _find_running_openvpn_pid, must not leave auth.txt behind on a 404.
        failure = RuntimeError("process table unreadable")
        logged = []
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=PEER), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=_not_found()), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid", side_effect=failure), \
                mock.patch.object(vpn_manager, "_log_poller_failure", side_effect=lambda m, e: logged.append((m, e))):
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
        self._assert_wiped(vpn_manager._SHARING_REVOKED_PEER_OFF)
        self.assertEqual(logged, [("VPN sharing revocation check failed", failure)])

    def test_real_disconnect_failure_still_wipes_on_peer_gone(self) -> None:
        failure = RuntimeError("process table unreadable")
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid", side_effect=failure), \
                mock.patch.object(vpn_manager, "_log_poller_failure") as logged:
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
        self._assert_wiped(vpn_manager._SHARING_REVOKED_PEER_GONE)
        logged.assert_called_once_with("VPN sharing revocation check failed", failure)

    def test_revoke_helper_wipes_then_reraises_disconnect_error(self) -> None:
        failure = RuntimeError("stop failed")
        with mock.patch.object(vpn_manager, "disconnect", side_effect=failure):
            with self.assertRaises(RuntimeError) as raised:
                vpn_manager._revoke_local_credentials(self.settings, vpn_manager._SHARING_REVOKED_PEER_OFF)
        self.assertIs(raised.exception, failure)
        self._assert_wiped(vpn_manager._SHARING_REVOKED_PEER_OFF)

    def test_base_exception_from_disconnect_still_wipes_before_propagating(self) -> None:
        with mock.patch.object(vpn_manager, "disconnect", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                vpn_manager._revoke_local_credentials(self.settings, vpn_manager._SHARING_REVOKED_PEER_GONE)
        self._assert_wiped(vpn_manager._SHARING_REVOKED_PEER_GONE)

    def test_disconnect_failure_with_credentials_already_absent_does_not_raise(self) -> None:
        vpn_manager.auth_path(self.settings).unlink()
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid", side_effect=RuntimeError("boom")), \
                mock.patch.object(vpn_manager, "_log_poller_failure") as logged:
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
        self._assert_wiped(vpn_manager._SHARING_REVOKED_PEER_GONE)
        logged.assert_called_once()

    def test_later_ticks_after_failed_disconnect_are_quiet_noops(self) -> None:
        # After one revocation the state has no credentials; later ticks must not
        # call disconnect again, re-log, or overwrite the original revocation reason.
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid", side_effect=RuntimeError("boom")), \
                mock.patch.object(vpn_manager, "_log_poller_failure") as logged:
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
            first = vpn_manager._load_state(self.settings)
        with mock.patch("app.transfer.local_network.get_paired_peer") as get_peer, \
                mock.patch.object(vpn_manager, "disconnect") as disconnect, \
                mock.patch.object(vpn_manager, "_log_poller_failure") as later_logged:
            for _ in range(3):
                self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
            get_peer.assert_not_called()
            disconnect.assert_not_called()
            later_logged.assert_not_called()
        self.assertEqual(logged.call_count, 1)
        after = vpn_manager._load_state(self.settings)
        self.assertEqual(after["revoked_reason"], first["revoked_reason"])
        self.assertEqual(after["revoked_at"], first["revoked_at"])

    def test_transient_error_with_failing_disconnect_leaves_credentials(self) -> None:
        # Disconnect is only reached on a confirmed revocation. A flaky peer must
        # not trigger disconnect() at all, so a failing one cannot strip a live VPN.
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=PEER), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=TimeoutError("slow")), \
                mock.patch.object(vpn_manager, "disconnect", side_effect=RuntimeError("boom")) as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
        disconnect.assert_not_called()
        self.assertTrue(vpn_manager.auth_path(self.settings).exists())
        self.assertTrue(vpn_manager._load_state(self.settings)["has_credentials"])


if __name__ == "__main__":
    unittest.main()
