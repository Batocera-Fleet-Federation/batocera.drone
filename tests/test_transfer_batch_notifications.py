"""Issue #146: asset transfers summarize as started/finished bell entries per batch.

Per-asset rows stay in the audit trail (for the email digest) and never reach the
bell. The bell gets one started and one finished entry for each batch.
"""

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import app.storage.audit_store as audit_store
from app.common.settings import Settings
from app.device import notifications
from app.drone_api import RomRepository
from app.transfer import transfer_batch
from app.transfer.download_manager import DownloadManager


def _settings(root: Path) -> Settings:
    with mock.patch.dict(
        "os.environ",
        {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "OVERMIND_DEVICE_ID": "target-a",
        },
        clear=True,
    ):
        return Settings.from_env()


def _manager(root: Path) -> DownloadManager:
    # Thread.start is patched so no worker claims queued jobs behind the test's back.
    with mock.patch("app.transfer.download_manager.Thread.start"):
        return DownloadManager(_settings(root), RomRepository(root / "roms", root / "bios"))


def _complete(manager: DownloadManager, job_id: str, *, name: str, size: int, system: str) -> None:
    """Run one job through _run_job with a fake successful fetch."""
    result = {
        "status": "completed",
        "relative_path": name,
        "asset_type": "rom",
        "downloaded_bytes": size,
        "bytes_transferred": size,
        "total_bytes": size,
    }
    with manager._lock:
        job = manager._jobs[job_id]
        job["status"] = "downloading"
        job["started_at"] = datetime.now(timezone.utc).isoformat()
        job["_started_mono"] = time.monotonic()
    with mock.patch.object(manager._selector, "fetch", return_value=result), \
            mock.patch("app.transfer.download_manager._kick_asset_metadata_sync_after_download"):
        manager._run_job(job_id)


def _fail(manager: DownloadManager, job_id: str) -> None:
    with manager._lock:
        job = manager._jobs[job_id]
        job["status"] = "downloading"
        job["started_at"] = datetime.now(timezone.utc).isoformat()
        job["_started_mono"] = time.monotonic()
    with mock.patch.object(manager._selector, "fetch", side_effect=ValueError("checksum mismatch")), \
            mock.patch("app.transfer.download_manager._kick_asset_metadata_sync_after_download"):
        manager._run_job(job_id)


def _wait_for_finished(settings: Settings) -> list:
    """The idle timer closes an upload batch on its own thread; wait for its finished entry."""
    deadline = time.monotonic() + 5
    while True:
        types = [item["event_type"] for item in audit_store.list_notifications_page(settings, limit=50)["items"]]
        if "asset_transfer_finished" in types or time.monotonic() > deadline:
            return [kind for kind in types if kind == "asset_transfer_finished"]
        time.sleep(0.02)


class TransferBatchBellTests(unittest.TestCase):
    def test_n_downloads_in_one_action_make_exactly_two_bell_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp) / "userdata")
            with mock.patch("app.transfer.download_manager._notifications.record_event") as record_event, \
                    mock.patch("app.transfer.download_manager._notifications.record_audit_event") as record_audit:
                with manager.transfer_batch(peer={"name": "BATOCERA-R5", "drone_id": "source-a"}):
                    jobs = [
                        manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", f"Game{i}.zip", expected_size=1000)
                        for i in range(14)
                    ]
                for index, job in enumerate(jobs):
                    _complete(manager, job["job_id"], name=f"Game{index}.zip", size=1000, system="snes")

            bell = [call.args[1] for call in record_event.call_args_list]
            self.assertEqual(bell, ["asset_transfer_started", "asset_transfer_finished"])
            # The digest still gets one audit row per asset.
            self.assertEqual(record_audit.call_count, 14)
            self.assertTrue(all(call.args[1] == "asset_downloaded" for call in record_audit.call_args_list))

            started = record_event.call_args_list[0].args
            self.assertIn("Downloading 14 assets from BATOCERA-R5 (snes, 13.7 KB).", started[3])
            finished = record_event.call_args_list[1].args
            self.assertIn("Download finished: 14 completed, 0 failed (13.7 KB).", finished[3])

    def test_started_entry_waits_for_the_selection_to_be_sealed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp) / "userdata")
            with mock.patch("app.transfer.download_manager._notifications.record_event") as record_event:
                with manager.transfer_batch(peer={"name": "BATOCERA-R5"}):
                    record_event.assert_not_called()
                    manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "A.zip", expected_size=10)
                    record_event.assert_not_called()
            self.assertEqual(
                [call.args[1] for call in record_event.call_args_list],
                ["asset_transfer_started"],
            )

    def test_failed_job_is_listed_in_the_finished_entry_not_its_own_bell_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp) / "userdata")
            with mock.patch("app.transfer.download_manager._notifications.record_event") as record_event, \
                    mock.patch("app.transfer.download_manager._notifications.record_audit_event") as record_audit:
                with manager.transfer_batch(peer={"name": "BATOCERA-R5"}):
                    good = manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "Good.zip", expected_size=100)
                    bad = manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "Bad.zip", expected_size=100)
                _complete(manager, good["job_id"], name="Good.zip", size=100, system="snes")
                _fail(manager, bad["job_id"])

            self.assertEqual(
                [call.args[1] for call in record_event.call_args_list],
                ["asset_transfer_started", "asset_transfer_finished"],
            )
            finished = record_event.call_args_list[1].args[3]
            self.assertIn("1 completed, 1 failed", finished)
            self.assertIn("Failed: Bad.zip.", finished)
            # The failed job writes no per-asset row, as before.
            self.assertEqual(record_audit.call_count, 1)

    def test_single_asset_gets_a_started_and_finished_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp) / "userdata")
            with mock.patch("app.transfer.download_manager._notifications.record_event") as record_event:
                with manager.transfer_batch(peer={"name": "BATOCERA-R5"}):
                    job = manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "Only.zip", expected_size=2048)
                manager.cancel(job["job_id"])
            self.assertEqual(
                [call.args[1] for call in record_event.call_args_list],
                ["asset_transfer_started", "asset_transfer_finished"],
            )
            self.assertIn("0 completed, 0 failed, 1 cancelled", record_event.call_args_list[1].args[3])

    def test_clear_queue_settles_the_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp) / "userdata")
            with mock.patch("app.transfer.download_manager._notifications.record_event") as record_event:
                with manager.transfer_batch(peer={"name": "BATOCERA-R5"}):
                    for index in range(3):
                        manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", f"G{index}.zip", expected_size=5)
                manager.clear_queue()
            self.assertEqual(
                [call.args[1] for call in record_event.call_args_list],
                ["asset_transfer_started", "asset_transfer_finished"],
            )
            self.assertIn("0 completed, 0 failed, 3 cancelled", record_event.call_args_list[1].args[3])

    def test_retry_is_its_own_batch_and_does_not_rejoin_the_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _manager(Path(tmp) / "userdata")
            with manager.transfer_batch(peer={"name": "BATOCERA-R5"}):
                job = manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "Retry.zip", expected_size=5)
            manager.cancel(job["job_id"])
            with mock.patch("app.transfer.download_manager._notifications.record_event") as record_event:
                manager.retry(job["job_id"])
            self.assertEqual([call.args[1] for call in record_event.call_args_list], ["asset_transfer_started"])


class TransferBatchSummaryTests(unittest.TestCase):
    def test_finished_entry_caps_failed_names_and_systems(self) -> None:
        batch = transfer_batch.TransferBatch("b1", "download", "BATOCERA-R5")
        for index, system in enumerate(("snes", "gba", "psx", "n64", "nes", "snes", "snes")):
            batch.add_selected(system=system, total_bytes=0)
            batch.record_outcome(f"j{index}", status="failed", name=f"Bad{index}.zip")
        self.assertEqual(batch.open_count, 0)
        message = batch.finished_event()["message"]
        self.assertIn("0 completed, 7 failed.", message)
        self.assertIn("Failed: Bad0.zip, Bad1.zip, Bad2.zip, Bad3.zip, Bad4.zip, and 2 more.", message)
        self.assertIn("(gba, n64, nes and 2 more)", batch.started_event()["message"])

    def test_outcome_is_counted_once_per_job(self) -> None:
        batch = transfer_batch.TransferBatch("b1", "download", "peer")
        batch.add_selected(system="snes", total_bytes=10)
        self.assertTrue(batch.record_outcome("j1", status="completed", name="A", size_bytes=10))
        self.assertFalse(batch.record_outcome("j1", status="completed", name="A", size_bytes=10))
        self.assertEqual(batch.completed, 1)
        self.assertEqual(batch.open_count, 0)


class TransferBatchDigestTests(unittest.TestCase):
    def test_bell_batch_rows_stay_out_of_the_digest_and_per_asset_rows_stay_in_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            manager = _manager(Path(tmp) / "userdata")
            with manager.transfer_batch(peer={"name": "BATOCERA-R5"}):
                jobs = [
                    manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", f"G{i}.zip", expected_size=10)
                    for i in range(3)
                ]
            for index, job in enumerate(jobs):
                _complete(manager, job["job_id"], name=f"G{index}.zip", size=10, system="snes")

            digest_rows = audit_store.list_unsent_events(settings, notifications.EVENT_TYPES, limit=None)
            self.assertEqual(sorted(row["event_type"] for row in digest_rows), ["asset_downloaded"] * 3)
            bell = audit_store.list_notifications_page(settings, limit=50)["items"]
            self.assertEqual(
                sorted(item["event_type"] for item in bell),
                ["asset_transfer_finished", "asset_transfer_started"],
            )


class UploadBatchTests(unittest.TestCase):
    def test_uploads_from_one_peer_group_into_one_pair_of_bell_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            batches = transfer_batch.UploadBatches(idle_seconds=0.05)
            tokens = [
                batches.begin(settings, peer_key="peer-a", peer_label="BATOCERA-R5", system="snes")
                for _ in range(3)
            ]
            batches.finish(tokens[0], status="completed", name="A.zip", size_bytes=100, system="snes")
            batches.finish(tokens[1], status="failed", name="B.zip", system="snes")
            batches.finish(tokens[2], status="completed", name="C.zip", size_bytes=200, system="snes")

            _wait_for_finished(settings)
            bell = audit_store.list_notifications_page(settings, limit=50)["items"]
            self.assertEqual(
                sorted(item["event_type"] for item in bell),
                ["asset_transfer_finished", "asset_transfer_started"],
            )
            finished = next(item for item in bell if item["event_type"] == "asset_transfer_finished")
            self.assertIn("2 completed, 1 failed (300 B).", finished["message"])
            self.assertIn("Failed: B.zip.", finished["message"])
            # Upload batches never feed the digest.
            self.assertEqual(audit_store.list_unsent_events(settings, notifications.EVENT_TYPES, limit=None), [])

    def test_a_new_upload_within_the_idle_gap_keeps_the_batch_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            batches = transfer_batch.UploadBatches(idle_seconds=0.3)
            first = batches.begin(settings, peer_key="peer-a", peer_label="BATOCERA-R5")
            batches.finish(first, status="completed", name="A.zip", size_bytes=1)
            second = batches.begin(settings, peer_key="peer-a", peer_label="BATOCERA-R5")
            time.sleep(0.5)
            self.assertEqual(
                [item["event_type"] for item in audit_store.list_notifications_page(settings, limit=50)["items"]],
                ["asset_transfer_started"],
            )
            batches.finish(second, status="completed", name="B.zip", size_bytes=1)
            self.assertEqual(_wait_for_finished(settings), ["asset_transfer_finished"])


if __name__ == "__main__":
    unittest.main()
