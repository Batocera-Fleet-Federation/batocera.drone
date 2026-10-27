"""Issue #619: a failed stop of a revoked tunnel is retried on later sharing-check ticks."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


SAMPLE_OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"


def _payload() -> dict:
    return {
        "config_filename": "client.ovpn",
        "config_text": SAMPLE_OVPN,
        "has_credentials": True,
        "username": "peer-user",
        "password": "secret",
    }


class StopErrorStatusRetryUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-619-stop-error-status",
        }, clear=True):
            settings = Settings.from_env()
        vpn_manager.import_from_peer(
            settings, _payload(), source_peer_id="source-peer", source_peer_name="Peer One",
        )
        return settings

    def _tick(self, settings: Settings, count: int, disconnect: mock.Mock) -> None:
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", disconnect), \
                contextlib.redirect_stderr(io.StringIO()):
            for _ in range(count):
                vpn_manager.check_sharing_revocation(settings)

    def test_error_status_stop_is_retried_on_a_later_tick_while_peer_gone(self) -> None:
        # Issue #619: "Failed stop of a revoked VPN tunnel is never retried on later sharing-check ticks".
        # disconnect() reports a refused SIGTERM as an error status instead of raising; that is still a
        # failed stop, so the next tick must try again.
        settings = self._imported_settings()
        refused = {"status": "error", "errors": ["Failed to stop openvpn (pid 42): Operation not permitted"]}
        disconnect = mock.Mock(side_effect=[refused, {"status": "disconnected"}])
        self._tick(settings, 2, disconnect)

        self.assertEqual(
            disconnect.call_count, 2,
            "a stop that returns an error status must be retried on the next tick, not treated as done",
        )

    def test_repeated_failed_stops_keep_being_retried_until_one_succeeds(self) -> None:
        # Issue #619: "Failed stop of a revoked VPN tunnel is never retried on later sharing-check ticks".
        # Every tick whose stop still fails must keep the retry pending.
        settings = self._imported_settings()
        disconnect = mock.Mock(side_effect=[
            RuntimeError("process table unreadable"),
            RuntimeError("process table unreadable"),
            {"status": "disconnected"},
        ])
        self._tick(settings, 3, disconnect)

        self.assertEqual(disconnect.call_count, 3)
        self.assertFalse(vpn_manager._load_state(settings)["disconnect_retry_pending"])

    def test_successful_retry_is_not_repeated_on_later_ticks(self) -> None:
        # Issue #619: "Failed stop of a revoked VPN tunnel is never retried on later sharing-check ticks".
        # Only a failed stop is retried; once a retry succeeds, later ticks must not stop again.
        settings = self._imported_settings()
        disconnect = mock.Mock(side_effect=[RuntimeError("process table unreadable"), {"status": "disconnected"}])
        self._tick(settings, 4, disconnect)

        self.assertEqual(disconnect.call_count, 2)


if __name__ == "__main__":
    unittest.main()
