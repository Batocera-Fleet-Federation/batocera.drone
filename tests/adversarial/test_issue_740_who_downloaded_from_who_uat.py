"""Issue #740: download/upload email + notification must show who got what from who.

Quoted requirement: "Currently when a drone downloads from another drone an
email correctly gets sent. However it does not show which drone downloaded from
which drone. Update the email and notification to be clear about
who/what/when/where."
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.device import smtp_manager
from app.transfer.download_manager import _peer_label
from app.transfer.transfer_batch import TransferBatch
from app.web.handlers_peer import _upload_peer_label


def _settings(root: Path) -> Settings:
    with mock.patch.dict(
        "os.environ",
        {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_DEVICE_ID": "issue-740-target",
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        },
        clear=True,
    ):
        return Settings.from_env()


class Issue740WhoDownloadedFromWhoUat(unittest.TestCase):
    def test_digest_download_summary_names_source(self) -> None:
        # Issue #740: "it does not show which drone downloaded from which
        # drone" -- the emailed digest must name the source drone.
        rows = [
            {
                "event_type": "asset_downloaded",
                "details": {
                    "system": "snes",
                    "source_drone_id": "source-a",
                    "source_drone_name": "BATOCERA-R5",
                    "size_bytes": 1024,
                },
            }
        ]
        self.assertIn(
            "from BATOCERA-R5",
            "\n".join(smtp_manager._asset_summary_lines(rows)),
        )

    def test_digest_upload_summary_names_target(self) -> None:
        # Issue #740: "Update the email ... to be clear about who/what/when/
        # where" -- the serving drone's email must name the downloading drone.
        rows = [
            {
                "event_type": "asset_uploaded",
                "details": {
                    "system": "snes",
                    "target_drone_id": "downloader-1",
                    "target_drone_name": "BATOCERA-R9",
                    "size_bytes": 512,
                },
            }
        ]
        self.assertIn(
            "to BATOCERA-R9",
            "\n".join(smtp_manager._asset_summary_lines(rows)),
        )

    def test_digest_upload_summary_falls_back_to_target_id(self) -> None:
        # Issue #740: "clear about who" -- an upload with no recorded peer name
        # must still identify the target by its drone id, never a blank "to".
        rows = [
            {
                "event_type": "asset_uploaded",
                "details": {"system": "snes", "target_drone_id": "downloader-1"},
            }
        ]
        text = "\n".join(smtp_manager._asset_summary_lines(rows))
        self.assertIn("to downloader-1", text)
        self.assertNotIn("to \n", text)

    def test_batch_finished_download_names_source_peer(self) -> None:
        # Issue #740: "Update the ... notification to be clear about
        # who/what/when/where" -- the finished bell entry names the source.
        batch = TransferBatch("batch-1", "download", "BATOCERA-R5")
        batch.add_selected(system="snes", total_bytes=14000)
        batch.record_outcome("item-1", status="completed", name="Game.zip",
                             size_bytes=14000, system="snes")
        self.assertIn("Download from BATOCERA-R5 finished",
                      batch.finished_event()["message"])

    def test_batch_finished_upload_names_target_peer(self) -> None:
        # Issue #740: "Update the ... notification to be clear about
        # who/what/when/where" -- the uploader's finished bell entry names the
        # downloading peer.
        batch = TransferBatch("batch-2", "upload", "BATOCERA-R9")
        batch.record_outcome("item-1", status="completed", name="Game.zip",
                             size_bytes=512)
        self.assertIn("Upload to BATOCERA-R9 finished",
                      batch.finished_event()["message"])

    def test_digest_body_names_sending_drone_and_timestamp(self) -> None:
        # Issue #740: "clear about who/what/when/where" -- the email body
        # identifies the sending drone (where) and stamps each entry (when).
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            subject, body = smtp_manager._compose_digest(
                [
                    {
                        "event_type": "swarm_peer_connected",
                        "created_at": "2026-01-02T03:04:05+00:00",
                        "title": "Paired with BATOCERA-R5",
                        "message": "paired",
                    }
                ],
                settings,
            )
        self.assertIn("issue-740-target", subject)
        self.assertIn("Drone: ", body.splitlines()[0])
        self.assertIn("2026-01-02T03:04:05+00:00", body)

    def test_peer_label_ignores_blank_name(self) -> None:
        # Issue #740: "Update the email and notification to be clear about
        # who..." -- a whitespace-only peer name must not render as a blank
        # "who" in the notification; the known drone id must win instead.
        self.assertEqual(
            "source-a",
            _peer_label({"name": "   ", "hostname": "  ", "drone_id": "source-a"}),
        )

    def test_upload_peer_label_ignores_blank_name(self) -> None:
        # Issue #740: "Update the email and notification to be clear about
        # who..." -- same blank-"who" guard on the uploader side.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            with mock.patch(
                "app.transfer.local_network.get_paired_peer",
                return_value={"name": "   ", "hostname": " "},
            ):
                self.assertEqual("dev-1", _upload_peer_label(settings, "dev-1"))


if __name__ == "__main__":
    unittest.main()
