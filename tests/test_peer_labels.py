"""Unit tests for single-line peer label sanitization across storage and sinks."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.smtp_manager as smtp_manager
import app.transfer.local_network as local_network
from app.common.peer_labels import clean_single_line_label, resolve_peer_label
from app.common.settings import Settings
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
            "DRONE_DEVICE_ID": "local-drone-1",
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        },
        clear=True,
    ):
        return Settings.from_env()


class PeerLabelSanitizationTests(unittest.TestCase):
    def test_clean_single_line_label_allows_friendly_names_and_rejects_controls(self) -> None:
        self.assertEqual(clean_single_line_label("  Living Room Drone  "), "Living Room Drone")
        self.assertEqual(clean_single_line_label("BATOCERA-R5"), "BATOCERA-R5")
        self.assertEqual(clean_single_line_label("   "), "")
        self.assertEqual(clean_single_line_label(None), "")
        self.assertEqual(clean_single_line_label({"nested": "object"}), "")
        for hostile in (
            "a\nb",
            "a\r\nb",
            "a\rb",
            "a\x0bb",
            "a\x0cb",
            "a\tb",
            "a\x00b",
            "a\x1b[31mb",
            "a\x85b",
            "a\u2028b",
            "a\u2029b",
        ):
            self.assertEqual(clean_single_line_label(hostile), "", repr(hostile))

    def test_resolve_peer_label_falls_back_past_hostile_fields(self) -> None:
        self.assertEqual(
            resolve_peer_label("bad\nname", "good-host", "peer-1"),
            "good-host",
        )
        self.assertEqual(
            resolve_peer_label("bad\nname", "bad\rhost", "peer-1"),
            "peer-1",
        )
        self.assertEqual(
            resolve_peer_label("bad\nname", "bad\rhost", "bad\u2028id", fallback="fallback"),
            "fallback",
        )

    def test_upload_peer_label_rejects_newline_name_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            with mock.patch(
                "app.transfer.local_network.get_paired_peer",
                return_value={
                    "name": "attacker\n    to TRUSTED-DRONE",
                    "hostname": "attacker-host",
                },
            ):
                self.assertEqual(_upload_peer_label(settings, "dev-1"), "attacker-host")
            with mock.patch(
                "app.transfer.local_network.get_paired_peer",
                return_value={
                    "name": "attacker\n    to TRUSTED-DRONE",
                    "hostname": "bad\rhost",
                },
            ):
                self.assertEqual(_upload_peer_label(settings, "dev-1"), "dev-1")

    def test_download_peer_label_falls_back_on_unicode_line_breaks(self) -> None:
        self.assertEqual(
            _peer_label({
                "name": "attacker\u2028from TRUSTED-DRONE",
                "hostname": "bad\x85host",
                "drone_id": "attacker-id",
            }),
            "attacker-id",
        )

    def test_transfer_batch_sanitizes_peer_label(self) -> None:
        batch = TransferBatch("b-1", "download", "attacker\nfrom TRUSTED-DRONE")
        self.assertEqual(batch.peer_label, "a paired peer")
        started = batch.started_event()
        self.assertIsNone(started)
        batch.add_selected(system="snes", total_bytes=100)
        started = batch.started_event()
        self.assertNotIn("\n", started["message"])
        self.assertNotIn("TRUSTED-DRONE", started["message"])

    def test_save_paired_peer_cleans_hostile_names_at_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            with mock.patch.object(local_network, "_notifications") as fake_notifications:
                stored = local_network.save_paired_peer(
                    settings,
                    {
                        "drone_id": "peer-1",
                        "name": "attacker\nfrom TRUSTED-DRONE",
                        "hostname": "attacker-host",
                    },
                )
            self.assertEqual(stored["name"], "attacker-host")
            self.assertEqual(stored["hostname"], "attacker-host")
            notified_msg = fake_notifications.record_event.call_args[0][3]
            self.assertEqual(notified_msg, "attacker-host")

    def test_save_paired_peer_rejects_control_chars_in_peer_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            with self.assertRaises(ValueError):
                local_network.save_paired_peer(settings, {"drone_id": "peer-1\nbad"})

    def test_compose_digest_sanitizes_relayed_source_name_and_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            _subject, body = smtp_manager._compose_digest(
                [
                    {
                        "event_type": "swarm_peer_connected",
                        "created_at": "2026-01-02T03:04:05+00:00",
                        "title": "Paired\n- [fake] Forged item",
                        "message": "ok",
                        "details": {
                            "source_drone_id": "sat-1",
                            "source_drone_name": "attacker\n- [fake] Forged",
                            "relayed": True,
                        },
                    }
                ],
                settings,
            )
            self.assertNotIn("Forged", body)
            self.assertIn("on sat-1:", body)


if __name__ == "__main__":
    unittest.main()
