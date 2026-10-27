"""A refused OS signal during peer revocation must cause a later stop retry."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.device import vpn_manager


class OsSignalFailureRetrySecurityTests(unittest.TestCase):
    def test_permission_error_signaling_openvpn_is_retried_on_later_tick(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.dict("os.environ", {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "adversarial-security-619",
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

                with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                        mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=321), \
                        mock.patch.object(vpn_manager.os, "kill", side_effect=PermissionError("denied")) as kill, \
                        contextlib.redirect_stderr(io.StringIO()):
                    vpn_manager.check_sharing_revocation(settings)
                    vpn_manager.check_sharing_revocation(settings)

                self.assertEqual(
                    kill.call_count,
                    2,
                    "a failed SIGTERM must be retried on the next tick after revoked credentials are wiped",
                )


if __name__ == "__main__":
    unittest.main()
