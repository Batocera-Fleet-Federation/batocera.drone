"""Adversarial UAT for #597: failure points inside the real disconnect() path.

Expected behavior, derived from the issue before reading the patch: a confirmed
revocation (source peer 404, or no longer paired) must remove imported
credentials even when stopping OpenVPN fails, whichever step of disconnect()
fails. The failure must still be reported (not silently dropped), and a second
failure during the credential state write must not stop the credential file
from being removed. Revocation must never leave the VPN connect lock held,
since that would wedge the next connect or disconnect. Any non-confirmed
outcome (5xx, 403, network errors) must never reach disconnect() or wipe
anything, even when disconnect() would fail.

Unlike test_issue_597_fresh_disconnect_finally_uat.py, these tests keep the
real disconnect() body and inject failures at its own process and state-write
steps rather than replacing disconnect() wholesale.
"""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


SAMPLE_OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"
PEER = {"drone_id": "peer-1", "name": "Peer One"}
OPENVPN_PID = 4242


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://peer/v1/api/peer/vpn/config", code, "status", None, None)


class Issue597RealDisconnectBoundariesUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-597-real-disconnect",
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

    def test_disconnect_failing_at_its_own_state_write_still_wipes(self) -> None:
        # disconnect() ends with its own _save_state(connected_at=None). If that
        # write fails after a real stop, the revocation finally must still wipe.
        real_save = vpn_manager._save_state_payload
        writes = []

        def flaky_save(*args, **kwargs):
            writes.append(args)
            if len(writes) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_save(*args, **kwargs)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid", side_effect=[OPENVPN_PID, None]), \
                mock.patch.object(vpn_manager.os, "kill") as kill, \
                mock.patch.object(vpn_manager, "_save_state_payload", side_effect=flaky_save), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
        kill.assert_called_once()
        self.assertIn("database is locked", stderr.getvalue())
        self._assert_wiped(vpn_manager._SHARING_REVOKED_PEER_GONE)

    def test_disconnect_kill_refused_returns_error_without_raising_and_still_wipes(self) -> None:
        # disconnect() reports a refused SIGTERM as a returned error dict, not an
        # exception. Revocation must not depend on which failure style it sees.
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=OPENVPN_PID), \
                mock.patch.object(vpn_manager.os, "kill", side_effect=PermissionError(1, "denied")):
            self.assertEqual(vpn_manager.disconnect(self.settings)["status"], "error")
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=OPENVPN_PID), \
                mock.patch.object(vpn_manager.os, "kill", side_effect=PermissionError(1, "denied")):
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
        self._assert_wiped(vpn_manager._SHARING_REVOKED_PEER_GONE)

    def test_stop_failure_and_state_write_failure_still_wipe_and_report_both(self) -> None:
        # Both the stop failure and the credential state write fail on the first
        # tick. The file must still be removed, the tick must not raise, and the
        # operator's report must carry the stop failure, not only the DB error.
        stop_failure = RuntimeError("stop failed: process table unreadable")
        state_failure = sqlite3.OperationalError("database is locked")
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", side_effect=stop_failure), \
                mock.patch.object(vpn_manager, "_save_state", side_effect=state_failure), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
        self.assertFalse(vpn_manager.auth_path(self.settings).exists())
        self.assertIn("stop failed", stderr.getvalue())
        self.assertIn("database is locked", stderr.getvalue())

    def test_state_write_failure_converges_on_the_next_tick(self) -> None:
        # The file is gone but the state still says has_credentials. The next tick
        # re-confirms the revocation and completes the state write; then it is quiet.
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", side_effect=RuntimeError("stop failed")), \
                mock.patch.object(vpn_manager, "_save_state", side_effect=sqlite3.OperationalError("locked")), \
                mock.patch.object(vpn_manager, "_log_poller_failure"):
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
        self.assertTrue(vpn_manager._load_state(self.settings)["has_credentials"])
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", return_value={"status": "not_running"}), \
                mock.patch.object(vpn_manager, "_log_poller_failure") as logged:
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
            logged.assert_not_called()
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
        self._assert_wiped(vpn_manager._SHARING_REVOKED_PEER_GONE)

    def test_connect_lock_is_released_after_failed_revocation(self) -> None:
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid", side_effect=RuntimeError("boom")), \
                mock.patch.object(vpn_manager, "_log_poller_failure"):
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
        acquired = vpn_manager._CONNECT_LOCK.acquire(timeout=1)
        self.assertTrue(acquired, "a failed revocation must not leave the VPN connect lock held")
        vpn_manager._CONNECT_LOCK.release()

    def test_non_confirmed_outcomes_never_reach_disconnect_or_wipe(self) -> None:
        outcomes = {
            "http-500": _http_error(500),
            "http-403": _http_error(403),
            "url-error": URLError("unreachable"),
            "conn-reset": ConnectionResetError("reset"),
        }
        for label, outcome in outcomes.items():
            with self.subTest(outcome=label):
                with mock.patch("app.transfer.local_network.get_paired_peer", return_value=PEER), \
                        mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=outcome), \
                        mock.patch.object(vpn_manager, "disconnect", side_effect=RuntimeError("boom")) as disconnect:
                    self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
                disconnect.assert_not_called()
                self.assertTrue(vpn_manager.auth_path(self.settings).exists())
                self.assertTrue(vpn_manager._load_state(self.settings)["has_credentials"])
                self.assertEqual(vpn_manager._load_state(self.settings)["revoked_reason"], "")


if __name__ == "__main__":
    unittest.main()
