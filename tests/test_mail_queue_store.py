"""Durability, idempotency, and retry semantics for outbound mail."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.storage import mail_queue_store


def _settings(root: Path) -> Settings:
    with mock.patch.dict(
        "os.environ",
        {
            "USERDATA_ROOT": str(root),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "mail-queue-test",
        },
        clear=True,
    ):
        return Settings.from_env()


class MailQueueStoreTests(unittest.TestCase):
    def test_relay_idempotency_key_creates_one_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            first = mail_queue_store.enqueue(
                settings,
                kind="test",
                subject="hello",
                body="one",
                source_drone_id="satellite-1",
                source_job_id="42",
            )
            second = mail_queue_store.enqueue(
                settings,
                kind="test",
                subject="duplicate",
                body="two",
                source_drone_id="satellite-1",
                source_job_id="42",
            )
            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(mail_queue_store.pending(settings)), 1)

    def test_failure_is_persisted_with_backoff_then_success_removes_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            job = mail_queue_store.enqueue(settings, kind="test", subject="hello", body="body")
            self.assertEqual([item["id"] for item in mail_queue_store.ready(settings)], [job["id"]])

            mail_queue_store.mark_failed(settings, job["id"], "SMTP offline")
            pending = mail_queue_store.pending(settings)
            self.assertEqual(pending[0]["status"], "error")
            self.assertEqual(pending[0]["attempts"], 1)
            self.assertIn("SMTP offline", pending[0]["last_error"])
            self.assertEqual(mail_queue_store.ready(settings), [])

            mail_queue_store.mark_sent(settings, job["id"])
            self.assertEqual(mail_queue_store.pending(settings), [])

    def test_relay_acknowledgement_is_terminal_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            job = mail_queue_store.enqueue(settings, kind="config_backup", subject="backup", body="body")
            mail_queue_store.mark_relayed(settings, [job["id"]])
            self.assertEqual(mail_queue_store.pending(settings), [])
            self.assertEqual(mail_queue_store.ready(settings), [])


if __name__ == "__main__":
    unittest.main()
