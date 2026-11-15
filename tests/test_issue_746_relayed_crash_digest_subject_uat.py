"""Integration and UAT coverage for issue #746: relayed crash titles in digest Subjects.

A paired satellite relaying a ``game_crash`` event with embedded line breaks or
control characters in its ``title`` must not break digest email serialization
or poison subsequent digests. Clean crash titles must still appear in full.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.smtp_manager as smtp_manager
from app.common.settings import Settings
from app.web import handlers_peer


_VALID_SETTINGS = {
    "host": "smtp.example.com",
    "port": 587,
    "use_starttls": True,
    "use_ssl": False,
    "username": "me@example.com",
    "password": "hunter2",
    "from_address": "drone@example.com",
    "recipient_email": "owner@example.com",
}


def _settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "digest-owner-746",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class _FakePeerHandler(handlers_peer.HandlersPeerMixin):
    def __init__(self, settings: Settings, requester_id: str = "satellite-746") -> None:
        self.settings = settings
        self.requester_id = requester_id
        self.response = None

    def _peer_request_authorized(self) -> bool:
        return True

    def _peer_requester_device_id(self) -> str:
        return self.requester_id

    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.response = (status_code, payload)


class RelayedCrashDigestSubjectUatTests(unittest.TestCase):
    def _configured_owner(self, root: Path) -> Settings:
        settings = _settings(root)
        smtp_manager.update_settings(settings, _VALID_SETTINGS)
        smtp_manager.set_sharing_enabled(settings, True)
        return settings

    def test_relayed_hostile_crash_title_delivers_single_line_subject_and_marks_emailed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._configured_owner(Path(tmp))
            handler = _FakePeerHandler(settings)
            peer = {"drone_id": "satellite-746", "name": "Arcade"}
            payload = {
                "source_drone_id": "satellite-746",
                "events": [
                    {
                        "source_event_id": "crash-1",
                        "event_type": "game_crash",
                        "title": "crash\r\nBcc: evil@example.com",
                        "message": "emulator exited",
                        "details": {"game": "OutRun", "system": "mame"},
                        "created_at": "2026-11-15T12:00:00+00:00",
                    }
                ],
            }
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer):
                handler._handle_peer_smtp_notifications(payload)
            self.assertEqual(handler.response[0], 202)
            self.assertEqual(handler.response[1]["accepted_event_ids"], ["crash-1"])

            stored = smtp_manager._audit_store.list_unsent_events(settings, ["game_crash"])
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["title"], "game_crash")

            queued = smtp_manager.send_digest_if_needed(settings)
            self.assertEqual(queued["status"], "queued")

            with mock.patch("smtplib.SMTP") as smtp_cls:
                client = smtp_cls.return_value
                processed = smtp_manager.process_outbound_mail_queue(settings)

            self.assertEqual(processed["sent_count"], 1)
            self.assertEqual(processed["failed_count"], 0)
            client.send_message.assert_called_once()
            sent_message = client.send_message.call_args.args[0]
            subject = sent_message["Subject"]
            self.assertNotIn("\n", subject)
            self.assertNotIn("\r", subject)
            self.assertNotIn("Bcc:", subject)
            self.assertIn("game_crash", subject)
            self.assertEqual(smtp_manager._audit_store.list_unsent_events(settings, ["game_crash"]), [])
            self.assertEqual(smtp_manager._mail_store.pending(settings), [])

    def test_preexisting_poison_crash_row_in_audit_log_does_not_break_digest_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._configured_owner(Path(tmp))
            smtp_manager._audit_store.insert_relayed_event(
                settings,
                "game_crash",
                "crash\nX-Injected: yes",
                source_drone_id="satellite-746",
                source_event_id="legacy-poison-1",
                message="segfault",
                details={"game": "OutRun", "system": "mame"},
                created_at="2026-11-15T12:00:00+00:00",
            )

            queued = smtp_manager.send_digest_if_needed(settings)
            self.assertEqual(queued["status"], "queued")

            with mock.patch("smtplib.SMTP") as smtp_cls:
                client = smtp_cls.return_value
                processed = smtp_manager.process_outbound_mail_queue(settings)

            self.assertEqual(processed["sent_count"], 1)
            self.assertEqual(processed["failed_count"], 0)
            sent_message = client.send_message.call_args.args[0]
            subject = sent_message["Subject"]
            self.assertNotIn("\n", subject)
            self.assertNotIn("\r", subject)
            self.assertNotIn("X-Injected", subject)
            self.assertEqual(smtp_manager._audit_store.list_unsent_events(settings, ["game_crash"]), [])

    def test_clean_relayed_crash_title_is_preserved_in_digest_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._configured_owner(Path(tmp))
            handler = _FakePeerHandler(settings)
            peer = {"drone_id": "satellite-746", "name": "Arcade"}
            payload = {
                "source_drone_id": "satellite-746",
                "events": [
                    {
                        "source_event_id": "crash-2",
                        "event_type": "game_crash",
                        "title": "Super Mario World (snes) crashed",
                        "message": "sigsegv",
                        "details": {"game": "Super Mario World", "system": "snes"},
                        "created_at": "2026-11-15T12:00:00+00:00",
                    }
                ],
            }
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer):
                handler._handle_peer_smtp_notifications(payload)
            smtp_manager.send_digest_if_needed(settings)
            with mock.patch("smtplib.SMTP") as smtp_cls:
                client = smtp_cls.return_value
                processed = smtp_manager.process_outbound_mail_queue(settings)
            self.assertEqual(processed["sent_count"], 1)
            sent_message = client.send_message.call_args.args[0]
            self.assertIn("Super Mario World (snes) crashed", sent_message["Subject"])

    def test_send_mail_sanitizes_direct_multiline_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._configured_owner(Path(tmp))
            with mock.patch("smtplib.SMTP") as smtp_cls:
                client = smtp_cls.return_value
                smtp_manager.send_mail(settings, "Line1\r\nLine2\u2028Line3", "body")
            sent_message = client.send_message.call_args.args[0]
            self.assertNotIn("\r", sent_message["Subject"])
            self.assertNotIn("\n", sent_message["Subject"])
            self.assertNotIn("\u2028", sent_message["Subject"])


if __name__ == "__main__":
    unittest.main()
