"""A revoked tunnel that survives the stop escalation must still be retried."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.device import vpn_manager


class UnverifiedSigkillRetrySecurityTests(unittest.TestCase):
    def test_openvpn_still_running_after_sigkill_is_retried_on_later_tick(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.dict("os.environ", {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "adversarial-security-619-sigkill",
                "DRONE_VPN_DIR": str(root / "vpn"),
            }, clear=True), \
                    mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install"):
                settings = Settings.from_env()
                vpn_manager.import_from_peer(settings, {
                    "config_filename": "client.ovpn",
                    "config_text": "client\ndev tun\nremote vpn.example 1194\nauth-user-pass\n",
                    "has_credentials": True,
                    "username": "peer-user",
                    "password": "secret",
                }, source_peer_id="source-peer")

                # The process table keeps listing the tunnel: SIGTERM and SIGKILL were
                # delivered but the process did not exit (e.g. stuck in teardown).
                with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                        mock.patch.object(vpn_manager, "VPN_DISCONNECT_GRACE_SECONDS", 0.0), \
                        mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=321), \
                        mock.patch.object(vpn_manager.os, "kill") as kill, \
                        contextlib.redirect_stderr(io.StringIO()):
                    vpn_manager.check_sharing_revocation(settings)
                    first_tick_signals = kill.call_count
                    vpn_manager.check_sharing_revocation(settings)

                self.assertGreater(first_tick_signals, 0)
                self.assertGreater(
                    kill.call_count,
                    first_tick_signals,
                    "a revoked tunnel still listed after SIGKILL must be signalled again on the next tick",
                )


if __name__ == "__main__":
    unittest.main()
