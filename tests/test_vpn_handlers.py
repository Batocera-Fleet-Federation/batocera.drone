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

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


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


class VpnAutoStartHandlerTests(unittest.TestCase):
    def test_enables_auto_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_vpn_auto_start({"enabled": True})
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertTrue(payload["auto_start"])
            self.assertTrue(vpn_manager._load_state(settings)["auto_start"])


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


if __name__ == "__main__":
    unittest.main()
