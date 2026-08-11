"""HTTP-level tests for the Tailscale sharing routes added to
HandlersNetworkMixin (admin: sharing toggle, pull-from-peer) and
HandlersPeerMixin (_handle_peer_tailnet_config). Business logic itself is
covered in test_tailnet_sharing.py -- these just verify the handler layer
calls into it correctly and maps results to the right HTTP status codes.
Mirrors test_vpn_handlers.py's shape closely.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import app.device.tailnet_service as tailnet_service
from app.drone_api import Settings
from app.web import handlers_network, handlers_peer


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "tailnet-handler-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def _fake_tailscale_cli() -> mock.MagicMock:
    cli = mock.MagicMock()
    cli.exists.return_value = True
    cli.__str__.return_value = "/userdata/system/tailscale/bin/tailscale"
    return cli


def _status_json_result() -> mock.Mock:
    payload = {
        "BackendState": "Running",
        "Self": {"TailscaleIPs": ["100.64.0.5"], "DNSName": "drone.tailxyz.ts.net.", "ID": "self-id"},
        "CurrentTailnet": {"Name": "example.ts.net"},
        "Peer": {},
    }
    return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")


def _fake_subprocess_run(cli_args, **_kwargs):
    if "status" in cli_args and "--json" in cli_args:
        return _status_json_result()
    return mock.Mock(returncode=0, stdout="", stderr="")


class _FakeHandler:
    def __init__(self, settings: Settings, *, body: bytes = b"") -> None:
        self.settings = settings
        self.rfile = mock.Mock()
        self.rfile.read.return_value = body
        self.response = None

    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.response = (status_code, payload)

    def _read_json_body(self) -> dict:
        try:
            return json.loads(self.rfile.read.return_value.decode("utf-8") or "{}")
        except Exception:
            return {}


def _handler(settings: Settings, **kwargs) -> _FakeHandler:
    class Handler(handlers_network.HandlersNetworkMixin, _FakeHandler):
        pass

    return Handler(settings, **kwargs)


class TailnetSharingHandlerTests(unittest.TestCase):
    def test_enables_sharing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
                tailnet_service.tailnet_enroll("tskey-auth-fake", settings)
            handler = _handler(settings)
            handler._handle_admin_tailnet_sharing({"enabled": True})
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertTrue(payload["sharing_enabled"])
            self.assertTrue(tailnet_service._load_sharing_state(settings)["sharing_enabled"])

    def test_rejects_sharing_for_imported_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
                tailnet_service.import_tailnet_from_peer(
                    settings, {"auth_key": "tskey-auth-shared", "enrolled": True}, source_peer_id="peer-1",
                )
            handler = _handler(settings)
            with self.assertRaises(ValueError):
                handler._handle_admin_tailnet_sharing({"enabled": True})


class TailnetPullFromPeerHandlerTests(unittest.TestCase):
    def test_requires_peer_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with self.assertRaises(ValueError):
                handler._handle_admin_tailnet_pull_from_peer({})

    def test_unknown_peer_is_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=None):
                handler._handle_admin_tailnet_pull_from_peer({"peer_id": "ghost"})
            status, _payload = handler.response
            self.assertEqual(status, 404)

    def test_peer_sharing_disabled_maps_404_to_friendly_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            error = HTTPError("https://peer/v1/api/peer/tailnet/config", 404, "not found", None, None)
            with mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=peer), \
                    mock.patch.object(handlers_network, "_peer_get_json_for_peer", side_effect=error):
                handler._handle_admin_tailnet_pull_from_peer({"peer_id": "peer-1"})
            status, payload = handler.response
            self.assertEqual(status, 404)
            self.assertIn("sharing", payload["error"].lower())

    def test_unreachable_peer_is_502(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            with mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=peer), \
                    mock.patch.object(handlers_network, "_peer_get_json_for_peer", side_effect=OSError("unreachable")):
                handler._handle_admin_tailnet_pull_from_peer({"peer_id": "peer-1"})
            status, _payload = handler.response
            self.assertEqual(status, 502)

    def test_successful_pull_enrolls_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            remote_payload = {"auth_key": "tskey-auth-shared", "enrolled": True}
            with mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=peer), \
                    mock.patch.object(handlers_network, "_peer_get_json_for_peer", return_value=(remote_payload, "https://peer")), \
                    mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
                handler._handle_admin_tailnet_pull_from_peer({"peer_id": "peer-1"})
            status, payload = handler.response
            self.assertEqual(status, 200)
            state = tailnet_service._load_sharing_state(settings)
            self.assertEqual(state["source_peer_id"], "peer-1")
            self.assertEqual(state["auth_key"], "tskey-auth-shared")


class _FakePeerHandler:
    """Minimal stand-in for RomRequestHandler's peer-serving surface,
    mirroring test_vpn_handlers.py's _FakePeerHandler pattern."""

    def __init__(self, settings: Settings, *, authorized: bool = True) -> None:
        self.settings = settings
        self._authorized = authorized
        self.response = None

    def _peer_request_authorized(self) -> bool:
        return self._authorized

    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.response = (status_code, payload)

    def log_message(self, *args, **kwargs) -> None:
        pass


def _peer_handler(settings: Settings, **kwargs) -> _FakePeerHandler:
    class Handler(_FakePeerHandler, handlers_peer.HandlersPeerMixin):
        pass

    return Handler(settings, **kwargs)


class TailnetPeerConfigHandlerTests(unittest.TestCase):
    def test_rejects_unauthorized_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _peer_handler(settings, authorized=False)
            handler._handle_peer_tailnet_config()
            self.assertIsNone(handler.response)

    def test_404_when_sharing_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
                tailnet_service.tailnet_enroll("tskey-auth-fake", settings)
            handler = _peer_handler(settings)
            handler._handle_peer_tailnet_config()
            self.assertEqual(handler.response[0], 404)

    def test_404_when_never_enrolled_even_if_sharing_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            tailnet_service._save_sharing_state(settings, sharing_enabled=True)  # bypass the normal setter
            handler = _peer_handler(settings)
            handler._handle_peer_tailnet_config()
            self.assertEqual(handler.response[0], 404)

    def test_200_with_auth_key_when_sharing_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
                tailnet_service.tailnet_enroll("tskey-auth-reusable", settings)
                tailnet_service.set_tailnet_sharing_enabled(settings, True)
                handler = _peer_handler(settings)
                handler._handle_peer_tailnet_config()
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertEqual(payload["auth_key"], "tskey-auth-reusable")
            self.assertTrue(payload["enrolled"])


if __name__ == "__main__":
    unittest.main()
