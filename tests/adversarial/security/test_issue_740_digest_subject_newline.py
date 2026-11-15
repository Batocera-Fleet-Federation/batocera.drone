"""A relayed crash title must not be able to forge or break the digest Subject.

Issue #740: "Update the email and notification to be clear about who
downloaded from who."

The digest body title is single-line-sanitized, but the digest Subject for a
crash digest embeds the stored crash title verbatim
(`crashes[0]['title']`). Relayed titles are peer-supplied
(`ingest_relayed_notifications` stores `raw["title"]` raw), so a paired peer
can store a title with an embedded newline. That newline lands in the email
Subject, where stdlib `email` raises HeaderParseError at send time -- the
digest job fails, its rows stay unemailed, and every later digest re-includes
the poison row. The Subject must therefore be a single physical line no
matter what a peer stored as a title.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.smtp_manager as smtp_manager
from app.common.settings import Settings


def _settings(root: Path) -> Settings:
    with mock.patch.dict(
        "os.environ",
        {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_DEVICE_ID": "local-drone-1",
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        },
        clear=True,
    ):
        return Settings.from_env()


def _crash_item(title: str) -> dict:
    return {
        "event_type": "game_crash",
        "created_at": "2026-01-02T03:04:05+00:00",
        "title": title,
        "message": "a game crashed",
        "details": {
            "source_drone_id": "attacker-id",
            "source_drone_name": "attacker-id",
            "relayed": True,
        },
    }


class DigestSubjectNewlineTests(unittest.TestCase):
    def test_digest_subject_has_no_line_breaks_with_hostile_crash_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            subject, _body = smtp_manager._compose_digest(
                [_crash_item("crash\nevil-forged-subject-line")],
                settings,
            )
            self.assertNotIn("\n", subject)
            self.assertNotIn("\r", subject)

    def test_digest_subject_has_no_line_breaks_with_crlf_crash_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            subject, _body = smtp_manager._compose_digest(
                [_crash_item("crash\r\nevil-forged-subject-line")],
                settings,
            )
            self.assertNotIn("\n", subject)
            self.assertNotIn("\r", subject)
            self.assertNotIn("evil-forged-subject-line", subject)


if __name__ == "__main__":
    unittest.main()
