import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.storage import music_scrape_job_items as items


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "MOVIES_ROOT": str(root / "movies"),
        "MUSIC_ROOT": str(root / "music"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "music-scrape-job-items-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class MusicScrapeJobItemsStoreTests(unittest.TestCase):
    def test_no_items_yet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            page = items.list_by_status(settings, items.STATUS_FAILED)
            self.assertEqual(page, {"total": 0, "limit": 200, "offset": 0, "items": []})
            self.assertEqual(items.entry_keys_by_status(settings, items.STATUS_FAILED), [])

    def test_record_then_list_by_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            items.record(settings, "aaaa", "Good Match.mp3", "Good Match.mp3", items.STATUS_MATCHED)
            items.record(settings, "bbbb", "No Match.mp3", "No Match.mp3", items.STATUS_FAILED, "no MusicBrainz results")
            items.record(settings, "cccc", "----.mp3", "----.mp3", items.STATUS_SKIPPED, "empty query")

            failed = items.list_by_status(settings, items.STATUS_FAILED)
            self.assertEqual(failed["total"], 1)
            self.assertEqual(failed["items"][0]["entry_key"], "bbbb")
            self.assertEqual(failed["items"][0]["reason"], "no MusicBrainz results")

            matched = items.list_by_status(settings, items.STATUS_MATCHED)
            self.assertEqual(matched["total"], 1)
            self.assertEqual(matched["items"][0]["entry_key"], "aaaa")

    def test_record_upserts_by_entry_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            items.record(settings, "aaaa", "Some Track.mp3", "Some Track.mp3", items.STATUS_FAILED, "no MusicBrainz results")
            items.record(settings, "aaaa", "Some Track.mp3", "Some Track.mp3", items.STATUS_MATCHED)

            self.assertEqual(items.list_by_status(settings, items.STATUS_FAILED)["total"], 0)
            matched = items.list_by_status(settings, items.STATUS_MATCHED)
            self.assertEqual(matched["total"], 1)
            self.assertEqual(matched["items"][0]["entry_key"], "aaaa")

    def test_clear_removes_every_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            items.record(settings, "aaaa", "A.mp3", "A.mp3", items.STATUS_MATCHED)
            items.record(settings, "bbbb", "B.mp3", "B.mp3", items.STATUS_FAILED, "no match")
            items.clear(settings)
            self.assertEqual(items.list_by_status(settings, items.STATUS_MATCHED)["total"], 0)
            self.assertEqual(items.list_by_status(settings, items.STATUS_FAILED)["total"], 0)

    def test_entry_keys_by_status_is_unpaged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            for i in range(5):
                items.record(settings, f"key{i}", f"Track{i}.mp3", f"Track{i}.mp3", items.STATUS_FAILED, "no match")
            keys = items.entry_keys_by_status(settings, items.STATUS_FAILED)
            self.assertEqual(sorted(keys), [f"key{i}" for i in range(5)])

    def test_list_by_status_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            for i in range(5):
                items.record(settings, f"key{i}", f"Track{i}.mp3", f"Track{i}.mp3", items.STATUS_FAILED, "no match")
            page = items.list_by_status(settings, items.STATUS_FAILED, limit=2, offset=2)
            self.assertEqual(page["total"], 5)
            self.assertEqual(len(page["items"]), 2)
            self.assertEqual(page["limit"], 2)
            self.assertEqual(page["offset"], 2)


if __name__ == "__main__":
    unittest.main()
