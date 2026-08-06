"""HTTP-level tests for HandlersNetworkShareMixin: request parsing and
status-code mapping. Business logic itself is covered in
test_network_share_manager.py -- these just verify the handler layer calls
into it correctly and maps results to the right HTTP status codes.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.network_share_manager as network_share_manager
from app.drone_api import Settings
from app.web import handlers_network_share


def _build_settings(test_case: unittest.TestCase, root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "network-share-handler-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class _FakeHandler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.response = None

    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.response = (status_code, payload)


def _handler(settings: Settings) -> _FakeHandler:
    class Handler(handlers_network_share.HandlersNetworkShareMixin, _FakeHandler):
        pass

    return Handler(settings)


class NetworkSharesListHandlerTests(unittest.TestCase):
    def test_list_delegates_to_manager_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(network_share_manager, "status", return_value=[{"peer_id": "p1"}]):
                handler._handle_admin_network_shares_list()
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"shares": [{"peer_id": "p1"}]})


class NetworkShareEnableHandlerTests(unittest.TestCase):
    def test_enable_returns_200_on_mounted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(network_share_manager, "enable", return_value={"status": "mounted", "peer_id": "p1"}):
                handler._handle_admin_network_share_enable("p1")
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "mounted")

    def test_enable_returns_502_on_mount_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(network_share_manager, "enable", return_value={"status": "error", "status_detail": "mount failed"}):
                handler._handle_admin_network_share_enable("p1")
            status, payload = handler.response
            self.assertEqual(status, 502)

    def test_enable_returns_400_when_peer_not_paired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(network_share_manager, "enable", side_effect=ValueError("not a paired peer")):
                handler._handle_admin_network_share_enable("unknown")
            status, payload = handler.response
            self.assertEqual(status, 400)
            self.assertIn("not a paired peer", payload["error"])

    def test_enable_unquotes_a_percent_encoded_mac_style_peer_id(self) -> None:
        # Regression: peer ids look like MAC addresses (e.g.
        # "58:47:ca:7e:38:57"); encodeURIComponent() on the client percent-
        # encodes the ":"s, and the stdlib server does not auto-decode path
        # segments -- without unquoting here, every real peer_id 404ed/failed
        # lookup against the (un-encoded) paired-peer map.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(network_share_manager, "enable", return_value={"status": "mounted", "peer_id": "58:47:ca:7e:38:57"}) as enable:
                handler._handle_admin_network_share_enable("58%3A47%3Aca%3A7e%3A38%3A57")
            enable.assert_called_once_with(settings, "58:47:ca:7e:38:57")


class NetworkShareDisableHandlerTests(unittest.TestCase):
    def test_disable_returns_200_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(network_share_manager, "disable", return_value={"status": "disabled", "peer_id": "p1"}):
                handler._handle_admin_network_share_disable("p1")
            status, payload = handler.response
            self.assertEqual(status, 200)

    def test_disable_unquotes_a_percent_encoded_mac_style_peer_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(network_share_manager, "disable", return_value={"status": "disabled", "peer_id": "58:47:ca:7e:38:57"}) as disable:
                handler._handle_admin_network_share_disable("58%3A47%3Aca%3A7e%3A38%3A57")
            disable.assert_called_once_with(settings, "58:47:ca:7e:38:57")

    def test_disable_returns_404_when_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(network_share_manager, "disable", return_value={"status": "not_found", "peer_id": "p1"}):
                handler._handle_admin_network_share_disable("p1")
            status, payload = handler.response
            self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
