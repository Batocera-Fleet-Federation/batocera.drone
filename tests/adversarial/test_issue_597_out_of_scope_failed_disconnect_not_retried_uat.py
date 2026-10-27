"""Issue #619: retry a failed disconnect on a later sharing-revocation tick."""

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


class FailedDisconnectRetryUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-597-out-of-scope",
        }, clear=True):
            settings = Settings.from_env()
        vpn_manager.import_from_peer(
            settings, _payload(), source_peer_id="source-peer", source_peer_name="Peer One",
        )
        return settings

    def test_failed_disconnect_is_retried_on_a_later_tick_while_peer_gone(self) -> None:
        # Issue #619: "Failed stop of a revoked VPN tunnel is never retried on later sharing-check ticks"
        # describes the defect; the expected behavior is another stop attempt on a later tick.
        settings = self._imported_settings()
        disconnect = mock.Mock(side_effect=[RuntimeError("process table unreadable"), {"status": "disconnected"}])
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", disconnect), \
                contextlib.redirect_stderr(io.StringIO()):
            vpn_manager.check_sharing_revocation(settings)
            vpn_manager.check_sharing_revocation(settings)

        self.assertEqual(
            disconnect.call_count, 2,
            "a failed stop of a revoked tunnel must be retried on the next tick, not forgotten",
        )


if __name__ == "__main__":
    unittest.main()
