import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.storage import movie_scrape_jobs as jobs


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "MOVIES_ROOT": str(root / "movies"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "movie-scrape-jobs-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class MovieScrapeJobsStoreTests(unittest.TestCase):
    def test_no_jobs_yet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertIsNone(jobs.latest(settings))
            self.assertFalse(jobs.any_running(settings))

    def test_create_running_then_latest_reflects_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = jobs.create_running(settings, rescan_all=True, total=5)
            self.assertEqual(row["status"], jobs.STATUS_RUNNING)
            self.assertTrue(row["rescan_all"])
            self.assertEqual(row["total"], 5)
            self.assertEqual(row["processed"], 0)

            latest = jobs.latest(settings)
            self.assertEqual(latest["id"], row["id"])
            self.assertEqual(latest["status"], jobs.STATUS_RUNNING)
            self.assertTrue(jobs.any_running(settings))

    def test_update_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = jobs.create_running(settings, rescan_all=False, total=3)
            jobs.update_progress(
                settings, row["id"],
                processed=1, current_movie="Some Movie.mp4",
                matched_count=1, skipped_count=0, failed_count=0,
            )
            latest = jobs.latest(settings)
            self.assertEqual(latest["processed"], 1)
            self.assertEqual(latest["current_movie"], "Some Movie.mp4")
            self.assertEqual(latest["matched_count"], 1)

    def test_mark_complete_clears_running_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = jobs.create_running(settings, rescan_all=False, total=1)
            jobs.mark_complete(settings, row["id"])
            latest = jobs.latest(settings)
            self.assertEqual(latest["status"], jobs.STATUS_COMPLETE)
            self.assertIsNotNone(latest["completed_at"])
            self.assertEqual(latest["current_movie"], "")
            self.assertFalse(jobs.any_running(settings))

    def test_mark_error_records_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = jobs.create_running(settings, rescan_all=False, total=0)
            jobs.mark_error(settings, row["id"], "No TMDb API key is configured")
            latest = jobs.latest(settings)
            self.assertEqual(latest["status"], jobs.STATUS_ERROR)
            self.assertEqual(latest["error_message"], "No TMDb API key is configured")
            self.assertFalse(jobs.any_running(settings))

    def test_stop_requested_defaults_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = jobs.create_running(settings, rescan_all=False, total=1)
            self.assertFalse(row["stop_requested"])
            self.assertFalse(jobs.is_stop_requested(settings, row["id"]))
            self.assertFalse(jobs.latest(settings)["stop_requested"])

    def test_request_stop_sets_the_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = jobs.create_running(settings, rescan_all=False, total=1)
            jobs.request_stop(settings, row["id"])
            self.assertTrue(jobs.is_stop_requested(settings, row["id"]))
            self.assertTrue(jobs.latest(settings)["stop_requested"])

    def test_is_stop_requested_false_for_unknown_job_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertFalse(jobs.is_stop_requested(settings, 99999))

    def test_mark_stopped_sets_status_and_clears_current_movie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = jobs.create_running(settings, rescan_all=False, total=5)
            jobs.update_progress(
                settings, row["id"], processed=2, current_movie="Some Movie.mp4",
                matched_count=1, skipped_count=1, failed_count=0,
            )
            jobs.request_stop(settings, row["id"])
            jobs.mark_stopped(settings, row["id"])
            latest = jobs.latest(settings)
            self.assertEqual(latest["status"], jobs.STATUS_STOPPED)
            self.assertEqual(latest["current_movie"], "")
            self.assertIsNotNone(latest["completed_at"])
            self.assertFalse(jobs.any_running(settings))
            self.assertEqual(latest["matched_count"], 1)
            self.assertEqual(latest["skipped_count"], 1)

    def test_latest_returns_most_recent_of_multiple_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            first = jobs.create_running(settings, rescan_all=False, total=1)
            jobs.mark_complete(settings, first["id"])
            second = jobs.create_running(settings, rescan_all=True, total=2)
            latest = jobs.latest(settings)
            self.assertEqual(latest["id"], second["id"])
            self.assertEqual(latest["status"], jobs.STATUS_RUNNING)


if __name__ == "__main__":
    unittest.main()
