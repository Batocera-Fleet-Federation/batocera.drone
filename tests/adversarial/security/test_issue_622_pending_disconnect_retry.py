"""Issue #622: a failed stop of a revoked VPN tunnel must stay pending until confirmed stopped."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


SAMPLE_OVPN = b"client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"


def _payload() -> dict:
    return {
        "config_filename": "client.ovpn",
        "config_text": SAMPLE_OVPN.decode("utf-8"),
        "has_credentials": True,
        "username": "peer-user",
        "password": "secret",
    }


class PendingDisconnectRetryTests(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-622-pending-disconnect",
        }, clear=True):
            settings = Settings.from_env()
        vpn_manager.import_from_peer(
            settings, _payload(), source_peer_id="source-peer", source_peer_name="Peer One",
        )
        return settings

    def test_stop_error_with_matched_message_still_sets_retry_pending(self) -> None:
        # Issue #622 remediation: "Track a pending-disconnect flag in state, set when the stop
        # failed and cleared only when the tunnel is confirmed stopped." A stop that raised is a
        # failed stop regardless of its message text. The retry is suppressed only by substring
        # match on "boom", "pid lookup failed" or "tunnel busy" in vpn_manager._revoke_local_credentials.
        settings = self._imported_settings()
        disconnect = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", disconnect), \
                contextlib.redirect_stderr(io.StringIO()):
            vpn_manager.check_sharing_revocation(settings)

        self.assertTrue(
            vpn_manager._load_state(settings)["disconnect_retry_pending"],
            "a failed stop of a revoked tunnel must leave the retry pending, whatever the error text",
        )

    def test_fresh_config_upload_does_not_clear_pending_stop_retry(self) -> None:
        # Issue #622 remediation: "set when the stop failed and cleared only when the tunnel is
        # confirmed stopped." save_uploaded_config() writes disconnect_retry_pending=False without
        # any disconnect confirmation, so a later admin upload or peer import drops the retry while
        # the revoked OpenVPN process is still running.
        settings = self._imported_settings()
        failed_stop = {"status": "error", "errors": ["Failed to stop openvpn (pid 42): Operation not permitted"]}
        disconnect = mock.Mock(return_value=failed_stop)
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", disconnect), \
                contextlib.redirect_stderr(io.StringIO()):
            vpn_manager.check_sharing_revocation(settings)
        self.assertTrue(vpn_manager._load_state(settings)["disconnect_retry_pending"])

        vpn_manager.save_uploaded_config(settings, "new.ovpn", SAMPLE_OVPN)
        self.assertTrue(
            vpn_manager._load_state(settings)["disconnect_retry_pending"],
            "an unconfirmed stop must not be forgotten by an unrelated config upload",
        )

        disconnect.reset_mock()
        disconnect.return_value = {"status": "disconnected"}
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", disconnect), \
                contextlib.redirect_stderr(io.StringIO()):
            vpn_manager.check_sharing_revocation(settings)

        self.assertEqual(
            disconnect.call_count, 1,
            "the poller must still retry the stop of the revoked tunnel after a config upload",
        )


if __name__ == "__main__":
    unittest.main()
