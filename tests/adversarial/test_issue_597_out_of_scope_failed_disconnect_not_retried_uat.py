"""Out-of-scope probe next to #597: a failed disconnect during revocation is never retried.

Not a blocking #597 assertion. #597 is about the credential wipe still happening
when disconnect() raises, and that is covered in test_issue_597_disconnect_failure_wipe_uat.
This probe is a separate defect: once _revoke_local_credentials has recorded the
revocation, has_credentials is False, so every later tick returns early and never
calls disconnect() again. If the first stop attempt failed, the revoked openvpn
process can stay up on the revoked credentials indefinitely.
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


class OutOfScopeFailedDisconnectNotRetriedUAT(unittest.TestCase):
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
        # Evidence: tick 1 disconnect() raises, the wipe lands, has_credentials becomes False.
        # Tick 2 (peer still gone) returns False before any disconnect attempt, so the stop
        # is never retried. Expected: a revoked tunnel whose stop failed is retried.
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
