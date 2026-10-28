"""Issue #622: a failed stop of a revoked VPN tunnel must be retried even when the secret wipe also fails."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


SAMPLE_OVPN = b"client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"
SOURCE_PEER_ID = "source-peer"


class UnlinkFailureStopRetryTests(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-622-unlink-failure-stop-retry",
        }, clear=True):
            settings = Settings.from_env()
        vpn_manager.import_from_peer(
            settings,
            {
                "config_filename": "client.ovpn",
                "config_text": SAMPLE_OVPN.decode("utf-8"),
                "has_credentials": True,
                "username": "peer-user",
                "password": "secret",
            },
            source_peer_id=SOURCE_PEER_ID,
            source_peer_name="Peer One",
        )
        return settings

    def test_failed_stop_stays_pending_when_secret_unlink_also_fails(self) -> None:
        # Issue #622 remediation: "Track a pending-disconnect flag in state, set when the stop
        # failed and cleared only when the tunnel is confirmed stopped. Have the poller retry
        # disconnect() while the flag is set, independent of has_credentials."
        # _revoke_local_credentials() re-raises an unlink OSError before _save_state() runs, so a
        # failed stop is never recorded and the later tick (peer unreachable) never retries it.
        settings = self._imported_settings()
        failed_stop = {"status": "error", "errors": ["Failed to stop openvpn (pid 42): Operation not permitted"]}
        disconnect = mock.Mock(return_value=failed_stop)
        unlink_failing_path = mock.Mock()
        unlink_failing_path.unlink.side_effect = OSError(16, "Device or resource busy")

        # Tick 1: the peer answers "not sharing" over pinned TLS, the stop fails, the wipe fails.
        revoked_by_peer = HTTPError("https://peer/v1/api/peer/vpn/config", 404, "not found", None, None)
        peer = {"drone_id": SOURCE_PEER_ID, "name": "Peer One"}
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=revoked_by_peer), \
                mock.patch("app.transfer.peer_connectivity.peer_http_error_is_authenticated", return_value=True), \
                mock.patch.object(vpn_manager, "disconnect", disconnect), \
                mock.patch.object(vpn_manager, "auth_path", return_value=unlink_failing_path), \
                contextlib.redirect_stderr(io.StringIO()):
            vpn_manager.check_sharing_revocation(settings)

        self.assertTrue(
            vpn_manager._load_state(settings)["disconnect_retry_pending"],
            "the failed stop must be recorded even though the secret wipe failed",
        )

        # Tick 2: the peer is unreachable (transient). The pending stop must still be retried.
        disconnect.reset_mock()
        disconnect.return_value = {"status": "disconnected"}
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=URLError("timed out")), \
                mock.patch.object(vpn_manager, "disconnect", disconnect), \
                contextlib.redirect_stderr(io.StringIO()):
            vpn_manager.check_sharing_revocation(settings)

        self.assertEqual(
            disconnect.call_count, 1,
            "a failed stop of a revoked tunnel must be retried on later ticks regardless of the peer's answer",
        )


if __name__ == "__main__":
    unittest.main()
