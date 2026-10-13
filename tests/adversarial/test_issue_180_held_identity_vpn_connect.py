"""Issue #180: held identity must block every path that can start OpenVPN."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.device import vpn_manager
from app.web import handlers_vpn


SAMPLE_OVPN = b"client\ndev tun\nremote vpn.example.net 1194\nauth-user-pass\n"


def _settings(case, root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "issue-180-adversarial",
    }
    patcher = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install")
    case.addCleanup(patcher.stop)
    patcher.start()
    with mock.patch.dict("os.environ", env, clear=True):
        settings = Settings.from_env()
    vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN)
    vpn_manager.save_credentials(settings, "user", "secret")
    return settings


class _HandlerBase:
    def __init__(self, settings):
        self.settings = settings
        self.response = None

    def _send_json(self, code, payload, **kwargs):
        self.response = (code, payload)


def _handler(settings):
    class Handler(handlers_vpn.HandlersVpnMixin, _HandlerBase):
        pass
    return Handler(settings)


class HeldIdentityVpnConnectTests(unittest.TestCase):
    def test_manual_connect_refuses_before_openvpn_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(self, Path(directory))
            with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=False), \
                    mock.patch.object(vpn_manager.subprocess, "run") as run:
                result = vpn_manager.connect(settings)
            run.assert_not_called()
            self.assertEqual(result["status"], "error")
            self.assertIs(result.get("identity_held"), True)
            self.assertTrue(result.get("errors"))

    def test_reconnect_refuses_without_dropping_existing_tunnel(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(self, Path(directory))
            with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=False), \
                    mock.patch.object(vpn_manager, "disconnect") as disconnect, \
                    mock.patch.object(vpn_manager, "connect") as connect:
                result = vpn_manager.reconnect(settings)
            disconnect.assert_not_called()
            connect.assert_not_called()
            self.assertTrue(result["identity_held"])

    def test_startup_and_self_heal_return_without_bootstrap_or_status_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(self, Path(directory))
            with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=False), \
                    mock.patch.object(vpn_manager, "bootstrap_vpn_from_swarm") as bootstrap, \
                    mock.patch.object(vpn_manager, "connect") as connect, \
                    mock.patch.object(vpn_manager, "status") as status, \
                    mock.patch.object(vpn_manager, "reconnect") as reconnect:
                vpn_manager.maybe_auto_connect(settings)
                result = vpn_manager.check_and_self_heal(settings)
            self.assertIsNone(result)
            bootstrap.assert_not_called()
            connect.assert_not_called()
            status.assert_not_called()
            reconnect.assert_not_called()

    def test_http_connect_and_reconnect_map_held_result_to_409(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(self, Path(directory))
            with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=False):
                for endpoint in ("_handle_admin_vpn_connect", "_handle_admin_vpn_reconnect"):
                    handler = _handler(settings)
                    getattr(handler, endpoint)()
                    code, payload = handler.response
                    self.assertEqual(code, 409)
                    self.assertTrue(payload["identity_held"])

    def test_disconnect_is_still_allowed_during_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(self, Path(directory))
            with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=False):
                result = vpn_manager.disconnect(settings)
            self.assertEqual(result["status"], "not_running")


if __name__ == "__main__":
    unittest.main()
