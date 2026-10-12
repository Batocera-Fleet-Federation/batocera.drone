"""User-level acceptance tests for issue #136: email digest floods during asset transfers.

A bulk transfer used to queue a digest on every poller tick (a game crash skipped
the interval) and listed every asset on its own line, capped at 200 per digest.
Expected behavior: one digest per configured interval, crashes included, and
transfer events summarized by count, system and size.

Drives the real upload record site (``RomRequestHandler._stream_file``), the real
poller (``run_audit_email_digest_poller``) and the real outbound-mail consumer.
Only the SMTP socket is replaced.
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.device import smtp_manager
from app.drone_api import RomRequestHandler, Settings


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


class _Stop(Exception):
    pass


class DigestDuringBulkTransferUatTests(unittest.TestCase):
    def _settings(self, root: Path) -> Settings:
        env = {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "digest-uat",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            return Settings.from_env()

    def _upload_handler(self, settings: Settings) -> RomRequestHandler:
        handler = object.__new__(RomRequestHandler)
        handler.settings = settings
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler._send_security_headers = mock.Mock()
        handler._peer_requester_device_id = mock.Mock(return_value="satellite-1")
        return handler

    def _run_poller_ticks(self, settings: Settings, ticks: int) -> None:
        count = []

        def sleep(_seconds):
            count.append(1)
            if len(count) > ticks:
                raise _Stop()

        with mock.patch.object(smtp_manager.time, "sleep", side_effect=sleep), \
                mock.patch.object(smtp_manager._crash_history, "ingest_new_crashes", return_value=0), \
                mock.patch.object(smtp_manager._usb_enumeration, "check_usb_enumeration_throttled", return_value=None):
            with self.assertRaises(_Stop):
                smtp_manager.run_audit_email_digest_poller(settings)

    def test_upload_records_system_and_size_for_the_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            rom = root / "Zelda.zip"
            rom.write_bytes(b"x" * 2048)
            handler = self._upload_handler(settings)
            handler._stream_file(
                rom,
                "application/zip",
                upload_meta={"asset_type": "rom", "system": "snes", "relative_path": "snes/Zelda.zip"},
            )
            self.assertEqual(handler.wfile.getvalue(), b"x" * 2048)
            rows = smtp_manager._audit_store.list_unsent_events(settings, ["asset_uploaded"], limit=None)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["message"], "snes/Zelda.zip")
            self.assertEqual(rows[0]["details"]["system"], "snes")
            self.assertEqual(rows[0]["details"]["size_bytes"], 2048)

    def test_backlog_of_thousands_becomes_one_short_digest_per_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            rom = root / "Game.zip"
            rom.write_bytes(b"x" * 1024)
            handler = self._upload_handler(settings)
            for _ in range(1200):
                handler._stream_file(
                    rom,
                    "application/zip",
                    upload_meta={"asset_type": "rom", "system": "snes", "relative_path": "snes/Game.zip"},
                )
            # A crash arrives during the same burst. It must wait for the interval too.
            smtp_manager._notifications.record_event(settings, "game_crash", "airbustr crashed")

            with mock.patch.object(smtp_manager, "send_mail") as send_mail:
                self._run_poller_ticks(settings, ticks=4)

            # Four poller ticks inside one interval: exactly one digest, one email.
            send_mail.assert_called_once()
            subject, body = send_mail.call_args.args[1], send_mail.call_args.args[2]
            self.assertIn("airbustr crashed (+1200 more)", subject)
            self.assertIn("Uploaded: 1,200 assets, 1.2 MB", body)
            self.assertIn("    snes: 1,200 assets, 1.2 MB", body)
            self.assertIn("airbustr crashed", body)
            self.assertLess(len(body.splitlines()), 20)
            self.assertEqual(smtp_manager._audit_store.list_unsent_events(
                settings, smtp_manager._notifications.EVENT_TYPES, limit=None,
            ), [])
            self.assertEqual(smtp_manager._mail_store.pending(settings), [])


if __name__ == "__main__":
    unittest.main()
