"""UAT for #598: the unattended poller and hostile check inputs.

The sharing-revocation poller is the production caller. A tick that sees a
stale 404 after a re-import must leave the new credentials in place and keep
running. The check itself never raises: a raising roster lookup, a 404-shaped
URLError, or a peer GET that returns 200 after a replacement import must not
wipe or disconnect.
"""

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


SOURCE_A = "source-a"
SOURCE_B = "source-b"
OVPN_A = "client\ndev tun\nproto udp\nremote vpn-a.example.net 1194\nauth-user-pass\n"
OVPN_B = "client\ndev tun\nproto udp\nremote vpn-b.example.net 1194\nauth-user-pass\n"


def _payload(username: str, password: str, config_text: str, filename: str = "client.ovpn") -> dict:
    return {
        "config_filename": filename,
        "config_text": config_text,
        "has_credentials": True,
        "username": username,
        "password": password,
    }


class Issue598PollerAndErrorBoundariesUAT(unittest.TestCase):
    def _settings(self) -> Settings:
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
            "DRONE_DEVICE_ID": "adversarial-598-poller",
            "DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS": "30",
        }, clear=True):
            settings = Settings.from_env()
        vpn_manager.import_from_peer(
            settings, _payload("user-a", "pass-a", OVPN_A),
            source_peer_id=SOURCE_A, source_peer_name="Peer A",
        )
        return settings

    def test_poller_tick_preserves_reimported_credentials_on_stale_404(self) -> None:
        settings = self._settings()
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(_generation: int) -> bool:
            return len(sleeps) > 1

        def import_b_then_404(*_args, **_kwargs):
            vpn_manager.import_from_peer(
                settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                source_peer_id=SOURCE_B, source_peer_name="Peer B",
            )
            raise HTTPError("https://source-a/v1/api/peer/vpn/config", 404, "Not Found", None, None)

        with mock.patch.object(vpn_manager, "poller_generation", return_value=7), \
                mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=import_b_then_404), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            vpn_manager.run_sharing_revocation_poller(settings)

        self.assertGreaterEqual(len(sleeps), 2)
        disconnect.assert_not_called()
        state = vpn_manager._load_state(settings)
        self.assertEqual(state["source_peer_id"], SOURCE_B)
        self.assertTrue(state["has_credentials"])
        self.assertEqual(state["revoked_reason"], "")
        self.assertEqual(vpn_manager.auth_path(settings).read_text(), "user-b\npass-b\n")

    def test_raising_roster_lookup_after_reimport_does_not_revoke(self) -> None:
        settings = self._settings()

        def import_b_then_raise(*_args, **_kwargs):
            vpn_manager.import_from_peer(
                settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                source_peer_id=SOURCE_B, source_peer_name="Peer B",
            )
            raise OSError("roster unreadable")

        with mock.patch("app.transfer.local_network.get_paired_peer", side_effect=import_b_then_raise), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        disconnect.assert_not_called()
        self.assertEqual(vpn_manager._load_state(settings)["source_peer_id"], SOURCE_B)
        self.assertEqual(vpn_manager.auth_path(settings).read_text(), "user-b\npass-b\n")

    def test_urlerror_message_containing_404_does_not_revoke(self) -> None:
        settings = self._settings()
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer",
                           side_effect=URLError("HTTP Error 404: Not Found")), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        disconnect.assert_not_called()
        self.assertTrue(vpn_manager.auth_path(settings).exists())
        self.assertEqual(vpn_manager._load_state(settings)["source_peer_id"], SOURCE_A)

    def test_still_shared_200_after_reimport_leaves_new_source(self) -> None:
        settings = self._settings()

        def import_b_then_200(*_args, **_kwargs):
            vpn_manager.import_from_peer(
                settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                source_peer_id=SOURCE_B, source_peer_name="Peer B",
            )
            return {"config_filename": "from-b.ovpn", "has_credentials": True}, "https://source-a"

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=import_b_then_200), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        disconnect.assert_not_called()
        state = vpn_manager._load_state(settings)
        self.assertEqual(state["source_peer_id"], SOURCE_B)
        self.assertEqual(vpn_manager.auth_path(settings).read_text(), "user-b\npass-b\n")

    def test_check_does_not_raise_when_disconnect_is_skipped_on_stale_404(self) -> None:
        settings = self._settings()

        def import_b_then_404(*_args, **_kwargs):
            vpn_manager.import_from_peer(
                settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                source_peer_id=SOURCE_B, source_peer_name="Peer B",
            )
            raise HTTPError("https://source-a/v1/api/peer/vpn/config", 404, "Not Found", None, None)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=import_b_then_404):
            try:
                result = vpn_manager.check_sharing_revocation(settings)
            except Exception as error:  # pragma: no cover - failure path
                self.fail(f"check_sharing_revocation raised {error!r}")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
