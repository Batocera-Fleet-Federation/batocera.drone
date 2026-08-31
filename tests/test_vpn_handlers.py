"""HTTP-level tests for HandlersVpnMixin: request parsing, status-code
mapping, and multipart upload wiring. Business logic itself is covered in
test_vpn_manager.py -- these just verify the handler layer calls into it
correctly and maps results to the right HTTP status codes.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings
from app.web import handlers_vpn


def _build_settings(test_case: unittest.TestCase, root: Path) -> Settings:
    """See test_vpn_manager.py's _build_settings -- vpn_dir() is pinned under
    this test's own tmp dir for its whole lifetime, not just during
    Settings.from_env()."""
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "vpn-handler-test",
    }
    patcher = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install-root")
    test_case.addCleanup(patcher.stop)
    patcher.start()
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class _FakeHandler:
    def __init__(self, settings: Settings, *, headers=None, body: bytes = b"") -> None:
        self.settings = settings
        self.headers = headers or {}
        self.rfile = mock.Mock()
        self.rfile.read.return_value = body
        self.wfile = mock.Mock()
        self.response = None
        self.streamed = None

    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.response = (status_code, payload)

    def _read_json_body(self) -> dict:
        try:
            return json.loads(self.rfile.read.return_value.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _stream_file(self, path, content_type, as_attachment=False, **kwargs) -> None:
        self.streamed = {"path": path, "content_type": content_type, "as_attachment": as_attachment}


def _handler(settings: Settings, **kwargs) -> _FakeHandler:
    from app.web import handlers_vpn

    class Handler(handlers_vpn.HandlersVpnMixin, _FakeHandler):
        pass

    return Handler(settings, **kwargs)


def _multipart_body(field_name: str, filename: str, content: bytes, boundary: str = "TESTBOUNDARY") -> bytes:
    return (
        f"--{boundary}\r\n".encode()
        + f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
        + b"Content-Type: application/octet-stream\r\n\r\n"
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )


SAMPLE_OVPN = b"client\ndev tun\nremote vpn.example.net 1194\nauth-user-pass\n"


class VpnStatusHandlerTests(unittest.TestCase):
    def test_status_delegates_to_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_vpn_status()
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "disconnected")
            self.assertIn("validation_errors", payload)


class VpnUploadHandlerTests(unittest.TestCase):
    def test_upload_rejects_non_multipart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings, headers={"Content-Type": "application/json", "Content-Length": "3"})
            with self.assertRaises(ValueError):
                handler._handle_admin_vpn_upload()

    def test_upload_success_saves_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            body = _multipart_body("config", "ProtonVPN.ovpn", SAMPLE_OVPN)
            handler = _handler(
                settings,
                headers={"Content-Type": "multipart/form-data; boundary=TESTBOUNDARY", "Content-Length": str(len(body))},
                body=body,
            )
            handler._handle_admin_vpn_upload()
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertEqual(payload["config_filename"], "ProtonVPN.ovpn")
            self.assertEqual(payload["protocol"], "UDP")
            self.assertTrue(vpn_manager.config_path(settings).is_file())

    def test_upload_with_no_files_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            body = f"--TESTBOUNDARY--\r\n".encode()
            handler = _handler(
                settings,
                headers={"Content-Type": "multipart/form-data; boundary=TESTBOUNDARY", "Content-Length": str(len(body))},
                body=body,
            )
            with self.assertRaises(ValueError):
                handler._handle_admin_vpn_upload()


class VpnCredentialsHandlerTests(unittest.TestCase):
    def test_saves_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_vpn_credentials({"username": "tokenuser", "password": "tokenpass"})
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertEqual(payload["username"], "tokenuser")

    def test_missing_password_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            with self.assertRaises(ValueError):
                handler._handle_admin_vpn_credentials({"username": "tokenuser"})


class VpnConnectDisconnectHandlerTests(unittest.TestCase):
    def test_connect_not_ready_is_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_vpn_connect()
            status, payload = handler.response
            self.assertEqual(status, 400)
            self.assertEqual(payload["status"], "error")

    def test_connect_ready_is_200(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "connect", return_value={"status": "connecting"}):
                handler = _handler(settings)
                handler._handle_admin_vpn_connect()
            self.assertEqual(handler.response[0], 200)

    def test_disconnect_not_running_is_200(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_vpn_disconnect()
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "not_running")

    def test_disconnect_error_is_500(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "disconnect", return_value={"status": "error", "errors": ["boom"]}):
                handler = _handler(settings)
                handler._handle_admin_vpn_disconnect()
            self.assertEqual(handler.response[0], 500)

    def test_reconnect_delegates_to_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "reconnect", return_value={"status": "connecting"}) as reconnect:
                handler = _handler(settings)
                handler._handle_admin_vpn_reconnect()
            reconnect.assert_called_once_with(settings)
            self.assertEqual(handler.response[0], 200)

    def test_reconnect_not_ready_is_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "reconnect", return_value={"status": "error", "errors": ["not ready"]}):
                handler = _handler(settings)
                handler._handle_admin_vpn_reconnect()
            self.assertEqual(handler.response[0], 400)


class VpnVerifyIpHandlerTests(unittest.TestCase):
    def test_success_is_200(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "check_public_ip", return_value={"ip": "203.0.113.9"}):
                handler = _handler(settings)
                handler._handle_admin_vpn_verify_ip()
            self.assertEqual(handler.response[0], 200)

    def test_failure_is_502(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "check_public_ip", return_value={"error": "timed out"}):
                handler = _handler(settings)
                handler._handle_admin_vpn_verify_ip()
            self.assertEqual(handler.response[0], 502)


class VpnLogDownloadHandlerTests(unittest.TestCase):
    def test_missing_log_is_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            with self.assertRaises(FileNotFoundError):
                handler._handle_admin_vpn_log_download()

    def test_existing_log_streams_as_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.vpn_dir(settings).mkdir(parents=True)
            vpn_manager.log_path(settings).write_text("log line\n", encoding="utf-8")
            handler = _handler(settings)
            handler._handle_admin_vpn_log_download()
            self.assertTrue(handler.streamed["as_attachment"])
            self.assertEqual(handler.streamed["content_type"], "text/plain")


class VpnSharingHandlerTests(unittest.TestCase):
    def test_enables_sharing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_vpn_sharing({"enabled": True})
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertTrue(payload["sharing_enabled"])
            self.assertTrue(vpn_manager._load_state(settings)["sharing_enabled"])


class VpnSelfHealHandlerTests(unittest.TestCase):
    def test_disables_self_heal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_vpn_self_heal({"enabled": False})
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertFalse(payload["self_heal_enabled"])
            self.assertFalse(vpn_manager._load_state(settings)["self_heal_enabled"])

    def test_defaults_to_enabled_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_vpn_status()
            _status, payload = handler.response
            self.assertTrue(payload["self_heal_enabled"])

    def test_rejects_sharing_for_imported_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.import_from_peer(
                settings,
                {"config_filename": "client.ovpn", "config_text": SAMPLE_OVPN.decode("utf-8"), "has_credentials": False},
                source_peer_id="peer-1",
                source_peer_name="Peer One",
            )
            handler = _handler(settings)
            with self.assertRaises(ValueError):
                handler._handle_admin_vpn_sharing({"enabled": True})


class VpnPullFromPeerHandlerTests(unittest.TestCase):
    def test_requires_peer_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            with self.assertRaises(ValueError):
                handler._handle_admin_vpn_pull_from_peer({})

    def test_unknown_peer_is_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(handlers_vpn._local_network, "get_paired_peer", return_value=None):
                handler._handle_admin_vpn_pull_from_peer({"peer_id": "ghost"})
            status, _payload = handler.response
            self.assertEqual(status, 404)

    def test_peer_sharing_disabled_maps_404_to_friendly_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            error = HTTPError("https://peer/v1/api/peer/vpn/config", 404, "not found", None, None)
            with mock.patch.object(handlers_vpn._local_network, "get_paired_peer", return_value=peer), \
                    mock.patch.object(handlers_vpn, "_peer_get_json_for_peer", side_effect=error):
                handler._handle_admin_vpn_pull_from_peer({"peer_id": "peer-1"})
            status, payload = handler.response
            self.assertEqual(status, 404)
            self.assertIn("sharing", payload["error"].lower())

    def test_unreachable_peer_is_502(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            with mock.patch.object(handlers_vpn._local_network, "get_paired_peer", return_value=peer), \
                    mock.patch.object(handlers_vpn, "_peer_get_json_for_peer", side_effect=OSError("unreachable")):
                handler._handle_admin_vpn_pull_from_peer({"peer_id": "peer-1"})
            status, _payload = handler.response
            self.assertEqual(status, 502)

    def test_successful_pull_imports_config_and_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            remote_payload = {
                "config_filename": "PeerShared.ovpn",
                "config_text": SAMPLE_OVPN.decode("utf-8"),
                "remotes": ["vpn.example.net 1194"],
                "has_credentials": True,
                "username": "peeruser",
                "password": "peerpass123",
            }
            with mock.patch.object(handlers_vpn._local_network, "get_paired_peer", return_value=peer), \
                    mock.patch.object(handlers_vpn, "_peer_get_json_for_peer", return_value=(remote_payload, "https://peer")):
                handler._handle_admin_vpn_pull_from_peer({"peer_id": "peer-1"})
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertTrue(payload["credentials_imported"])
            self.assertTrue(vpn_manager.config_path(settings).is_file())
            self.assertEqual(vpn_manager.auth_path(settings).read_text(), "peeruser\npeerpass123\n")


class VpnImportFolderListHandlerTests(unittest.TestCase):
    def test_returns_directory_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            directory = vpn_manager.vpn_import_dir(settings)
            directory.mkdir(parents=True)
            (directory / "provider.ovpn").write_bytes(SAMPLE_OVPN)
            handler = _handler(settings)
            handler._handle_admin_vpn_import_folder_list()
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertEqual(payload["files"], ["provider.ovpn"])
            self.assertEqual(payload["directory"], str(directory))


class VpnImportFolderApplyHandlerTests(unittest.TestCase):
    def test_requires_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            with self.assertRaises(ValueError):
                handler._handle_admin_vpn_import_folder_apply({})

    def test_happy_path_is_200(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            directory = vpn_manager.vpn_import_dir(settings)
            directory.mkdir(parents=True)
            (directory / "provider.ovpn").write_bytes(SAMPLE_OVPN)
            handler = _handler(settings)
            handler._handle_admin_vpn_import_folder_apply({"filename": "provider.ovpn"})
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertEqual(payload["config_filename"], "provider.ovpn")

    def test_unknown_filename_is_404_not_500(self) -> None:
        # The load-bearing regression case: api_routes.py's generic POST
        # exception mapping only auto-maps ValueError -> 400, not
        # FileNotFoundError -> 404 (that's GET-only) -- the handler must
        # catch it explicitly, or this would come back as a 500.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_vpn_import_folder_apply({"filename": "does-not-exist.ovpn"})
            status, payload = handler.response
            self.assertEqual(status, 404)
            self.assertIn("error", payload)

    def test_traversal_filename_is_404_not_500(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_vpn_import_folder_apply({"filename": "../../etc/passwd"})
            status, _payload = handler.response
            self.assertEqual(status, 404)


class _FakePeerHandler:
    """Minimal stand-in for RomRequestHandler's peer-serving surface, mirroring
    the _FakePeerHandler pattern in test_movies_transfer.py."""

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
    from app.web import handlers_peer

    class Handler(_FakePeerHandler, handlers_peer.HandlersPeerMixin):
        pass

    return Handler(settings, **kwargs)


class VpnPeerConfigHandlerTests(unittest.TestCase):
    def test_rejects_unauthorized_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _peer_handler(settings, authorized=False)
            handler._handle_peer_vpn_config()
            self.assertIsNone(handler.response)

    def test_404_when_sharing_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN)
            vpn_manager.save_credentials(settings, "user", "pass")
            handler = _peer_handler(settings)
            handler._handle_peer_vpn_config()
            self.assertEqual(handler.response[0], 404)

    def test_404_when_no_config_even_if_sharing_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.set_sharing_enabled(settings, True)
            handler = _peer_handler(settings)
            handler._handle_peer_vpn_config()
            self.assertEqual(handler.response[0], 404)

    def test_200_with_config_and_credentials_when_sharing_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN)
            vpn_manager.save_credentials(settings, "tokenuser", "tokenpass123")
            vpn_manager.set_sharing_enabled(settings, True)
            handler = _peer_handler(settings)
            handler._handle_peer_vpn_config()
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertTrue(payload["has_credentials"])
            self.assertEqual(payload["username"], "tokenuser")
            self.assertEqual(payload["password"], "tokenpass123")


if __name__ == "__main__":
    unittest.main()
