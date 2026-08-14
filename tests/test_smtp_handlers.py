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
from app.web import handlers_peer, handlers_smtp


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


class _FakePeerHandler(_FakeHandler):
    def __init__(self, settings: Settings, *, authorized=True, requester_id="satellite-1") -> None:
        super().__init__(settings)
        self.authorized = authorized
        self.requester_id = requester_id

    def _peer_request_authorized(self) -> bool:
        return self.authorized

    def _peer_requester_device_id(self):
        return self.requester_id


def _handler(settings: Settings) -> _FakeHandler:
    class Handler(handlers_smtp.HandlersSmtpMixin, _FakeHandler):
        pass

    return Handler(settings)


def _peer_handler(settings: Settings, **kwargs) -> _FakePeerHandler:
    class Handler(_FakePeerHandler, handlers_peer.HandlersPeerMixin):
        pass

    return Handler(settings, **kwargs)


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


class SmtpTestHandlerTests(unittest.TestCase):
    def test_test_email_is_accepted_into_queue_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, {
                "host": "smtp.example.com",
                "from_address": "drone@example.com",
                "recipient_email": "owner@example.com",
            })
            handler = _handler(settings)
            with mock.patch.object(smtp_manager, "send_mail") as direct_send:
                handler._handle_admin_smtp_test()
            direct_send.assert_not_called()
            status, payload = handler.response
            self.assertEqual(status, 202)
            self.assertEqual(payload["status"], "queued")
            self.assertEqual(len(smtp_manager._mail_store.pending(settings)), 1)

    def test_test_email_requires_configuration_before_queueing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_smtp_test()
            status, payload = handler.response
            self.assertEqual(status, 400)
            self.assertEqual(payload["status"], "not_configured")


class SmtpNotificationRelayHandlerTests(unittest.TestCase):
    def _owner_settings(self, root: Path) -> Settings:
        settings = _build_settings(root)
        smtp_manager.update_settings(settings, {
            "host": "smtp.example.com",
            "from_address": "drone@example.com",
            "recipient_email": "owner@example.com",
        })
        smtp_manager.set_sharing_enabled(settings, True)
        return settings

    def test_rejects_unauthorized_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = _peer_handler(self._owner_settings(Path(tmp)), authorized=False)
            handler._handle_peer_smtp_notifications({"events": []})
            self.assertIsNone(handler.response)

    def test_rejects_claimed_id_that_does_not_match_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = _peer_handler(self._owner_settings(Path(tmp)))
            handler._handle_peer_smtp_notifications({
                "source_drone_id": "impostor",
                "events": [],
            })
            self.assertEqual(handler.response[0], 403)

    def test_accepts_idempotent_event_batch_from_paired_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._owner_settings(Path(tmp))
            handler = _peer_handler(settings)
            peer = {"drone_id": "satellite-1", "name": "Living Room"}
            payload = {
                "source_drone_id": "satellite-1",
                "events": [{
                    "source_event_id": "99",
                    "event_type": "asset_downloaded",
                    "title": "Asset downloaded",
                    "message": "Zelda.zip",
                    "created_at": "2026-08-14T12:00:00+00:00",
                }],
            }
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer):
                handler._handle_peer_smtp_notifications(payload)
                self.assertEqual(handler.response[0], 202)
                handler._handle_peer_smtp_notifications(payload)
            self.assertEqual(handler.response[0], 202)
            items = smtp_manager._audit_store.list_unsent_events(settings, ["asset_downloaded"])
            self.assertEqual(len(items), 1)


class SmtpMailRelayHandlerTests(unittest.TestCase):
    def _owner_settings(self, root: Path) -> Settings:
        settings = _build_settings(root)
        smtp_manager.update_settings(settings, {
            "host": "smtp.example.com",
            "from_address": "drone@example.com",
            "recipient_email": "owner@example.com",
        })
        smtp_manager.set_sharing_enabled(settings, True)
        return settings

    def test_accepts_idempotent_mail_batch_from_paired_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._owner_settings(Path(tmp))
            handler = _peer_handler(settings)
            peer = {"drone_id": "satellite-1", "name": "Living Room"}
            payload = {
                "source_drone_id": "satellite-1",
                "jobs": [{
                    "source_job_id": "99",
                    "kind": "test",
                    "subject": "Satellite test",
                    "body": "body",
                    "created_at": "2026-08-14T12:00:00+00:00",
                }],
            }
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer):
                handler._handle_peer_smtp_mail(payload)
                self.assertEqual(handler.response[0], 202)
                handler._handle_peer_smtp_mail(payload)
            self.assertEqual(handler.response[0], 202)
            self.assertEqual(len(smtp_manager._mail_store.pending(settings)), 1)

    def test_rejects_mail_claimed_id_that_does_not_match_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = _peer_handler(self._owner_settings(Path(tmp)))
            handler._handle_peer_smtp_mail({"source_drone_id": "impostor", "jobs": []})
            self.assertEqual(handler.response[0], 403)


if __name__ == "__main__":
    unittest.main()
