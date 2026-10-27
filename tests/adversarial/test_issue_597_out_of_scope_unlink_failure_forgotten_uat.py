"""Out-of-scope probe next to #597: a failed credential unlink is recorded as a completed wipe.

Not a blocking #597 assertion. #597 is about disconnect() raising; this is the other
failure inside the same wipe. _revoke_local_credentials swallows an OSError from
auth_path().unlink() and then still saves has_credentials=False. Every later tick
returns early on has_credentials=False, so the unlink is never retried and the
credentials file stays on disk for good.
"""

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


class OutOfScopeUnlinkFailureForgottenUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-597-unlink",
        }, clear=True):
            settings = Settings.from_env()
        vpn_manager.import_from_peer(
            settings, _payload(), source_peer_id="source-peer", source_peer_name="Peer One",
        )
        return settings

    def test_failed_unlink_is_retried_instead_of_being_forgotten(self) -> None:
        settings = self._imported_settings()
        auth = vpn_manager.auth_path(settings)
        real_unlink = Path.unlink
        failing = {"on": True}

        def unlink(path, missing_ok=False):
            if path == auth and failing["on"]:
                raise PermissionError(13, "Permission denied", str(path))
            return real_unlink(path, missing_ok=missing_ok)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=None), \
                mock.patch.object(Path, "unlink", unlink), \
                contextlib.redirect_stderr(io.StringIO()):
            vpn_manager.check_sharing_revocation(settings)
            self.assertTrue(auth.exists(), "precondition: the first unlink really failed")

            failing["on"] = False
            vpn_manager.check_sharing_revocation(settings)

        self.assertFalse(
            auth.exists(),
            "a failed credential unlink must be retried; the state must not claim the wipe completed",
        )


if __name__ == "__main__":
    unittest.main()
