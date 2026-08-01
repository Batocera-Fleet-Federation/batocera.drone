"""HTTP-level tests for HandlersSmtpMixin's digest-interval endpoint: request
parsing and status-code mapping. Business logic itself is covered in
test_smtp_manager.py -- this just verifies the handler layer calls into it
correctly and maps results to the right HTTP status codes (mirrors
test_vpn_handlers.py's shape).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.smtp_manager as smtp_manager
from app.drone_api import Settings
from app.web import handlers_smtp


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "smtp-handler-test",
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
    class Handler(handlers_smtp.HandlersSmtpMixin, _FakeHandler):
        pass

    return Handler(settings)


class SmtpDigestIntervalHandlerTests(unittest.TestCase):
    def test_saves_a_valid_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_smtp_digest_interval_update({"digest_interval_seconds": 900})
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertEqual(payload["digest_interval_seconds"], 900)
            self.assertEqual(smtp_manager._load_state(settings)["digest_interval_seconds"], 900)

    def test_out_of_range_value_is_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_smtp_digest_interval_update({"digest_interval_seconds": 30})
            status, payload = handler.response
            self.assertEqual(status, 400)
            self.assertIn("error", payload)

    def test_missing_payload_is_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_smtp_digest_interval_update({})
            status, _payload = handler.response
            self.assertEqual(status, 400)

    def test_saved_value_reflected_in_status_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_smtp_digest_interval_update({"digest_interval_seconds": 3600})
            handler._handle_admin_smtp_status()
            _status, payload = handler.response
            self.assertEqual(payload["digest_interval_seconds"], 3600)


if __name__ == "__main__":
    unittest.main()
