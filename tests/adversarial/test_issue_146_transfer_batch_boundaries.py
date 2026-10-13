"""Adversarial acceptance for issue #146 transfer batch boundaries.

Expectations come from the issue: one action has at most a started/finished
pair, even when jobs terminate before the selection is sealed; empty previews
are not transfers; per-asset success rows remain digest input while batch rows
are bell-only; and separate upload peers are independent batches.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.device import notifications
from app.storage import audit_store
from app.transfer import transfer_batch
from app.transfer.download_manager import DownloadManager
from app.drone_api import RomRepository


def settings_at(root):
    with mock.patch.dict("os.environ", {
        "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "issue-146-adversarial",
    }, clear=True):
        return Settings.from_env()


def manager_at(root):
    with mock.patch("app.transfer.download_manager.Thread.start"):
        return DownloadManager(settings_at(root), RomRepository(root / "roms", root / "bios"))


class DownloadBatchBoundaryTests(unittest.TestCase):
    def test_terminal_before_scope_exit_still_emits_ordered_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = manager_at(Path(tmp))
            with mock.patch("app.transfer.download_manager._notifications.record_event") as record:
                with manager.transfer_batch(peer={"name": "peer"}):
                    job = manager.enqueue_rom({}, {"drone_id": "peer"}, "snes", "lost.zip", expected_size=17)
                    manager.cancel(job["job_id"])
                types = [call.args[1] for call in record.call_args_list]
            self.assertEqual(types, ["asset_transfer_started", "asset_transfer_finished"])
            self.assertIn("1 asset", record.call_args_list[0].args[3])
            self.assertIn("1 cancelled", record.call_args_list[1].args[3])

    def test_empty_selection_emits_no_bell_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = manager_at(Path(tmp))
            with mock.patch("app.transfer.download_manager._notifications.record_event") as record:
                with manager.transfer_batch(peer={"name": "peer"}):
                    pass
            record.assert_not_called()

    def test_audit_only_asset_rows_are_digest_visible_but_stage_rows_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_at(Path(tmp))
            notifications.record_audit_event(settings, "asset_downloaded", "Asset downloaded", "game.zip",
                                              {"system": "snes", "size_bytes": 25})
            notifications.record_event(settings, "asset_transfer_started", "Started", "Downloading 1 asset")
            notifications.record_event(settings, "asset_transfer_finished", "Finished", "1 completed")
            unsent = audit_store.list_unsent_events(settings, notifications.EVENT_TYPES, limit=None)
            bell = audit_store.list_notifications_page(settings, limit=20)["items"]
            self.assertEqual([row["event_type"] for row in unsent], ["asset_downloaded"])
            self.assertEqual(sorted(row["event_type"] for row in bell),
                             ["asset_transfer_finished", "asset_transfer_started"])


class UploadPeerIsolationTests(unittest.TestCase):
    def test_interleaved_peers_get_independent_started_and_finished_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_at(Path(tmp))
            batches = transfer_batch.UploadBatches(idle_seconds=0.01)
            a = batches.begin(settings, peer_key="a", peer_label="Peer A", system="snes")
            b = batches.begin(settings, peer_key="b", peer_label="Peer B", system="gba")
            batches.finish(a, status="failed", name="broken.zip", system="snes")
            batches.finish(b, status="completed", name="ok.zip", size_bytes=11, system="gba")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                rows = audit_store.list_notifications_page(settings, limit=20)["items"]
                if sum(row["event_type"] == "asset_transfer_finished" for row in rows) == 2:
                    break
                time.sleep(0.01)
            self.assertEqual(sorted(row["event_type"] for row in rows), [
                "asset_transfer_finished", "asset_transfer_finished",
                "asset_transfer_started", "asset_transfer_started",
            ])
            finished = [row["message"] for row in rows if row["event_type"] == "asset_transfer_finished"]
            self.assertTrue(any("1 failed" in message and "broken.zip" in message for message in finished))
            self.assertTrue(any("1 completed" in message for message in finished))
            self.assertEqual(audit_store.list_unsent_events(settings, notifications.EVENT_TYPES, limit=None), [])


if __name__ == "__main__":
    unittest.main()
