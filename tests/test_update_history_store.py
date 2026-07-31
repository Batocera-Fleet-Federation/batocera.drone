import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.storage.update_history_store as update_history_store
from app.common.settings import Settings


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "update-history-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class RecordUpdateTests(unittest.TestCase):
    def test_records_and_lists_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            first = update_history_store.record_update(
                settings,
                version="v0.1.98",
                previous_version="v0.1.97",
                release_url="https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/tag/v0.1.98",
                release_notes="- did a thing (abc1234)",
            )
            second = update_history_store.record_update(
                settings, version="v0.1.99", previous_version="v0.1.98", release_notes="- did another thing"
            )
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)

            rows = update_history_store.list_updates(settings)
            self.assertEqual([row["version"] for row in rows], ["v0.1.99", "v0.1.98"])
            self.assertEqual(rows[1]["previous_version"], "v0.1.97")
            self.assertEqual(rows[1]["release_notes"], "- did a thing (abc1234)")
            self.assertIn("releases/tag/v0.1.98", rows[1]["release_url"])
            self.assertIsNotNone(rows[0]["applied_at"])

    def test_missing_optional_fields_default_to_empty_strings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            update_history_store.record_update(settings, version="v0.1.99")
            row = update_history_store.list_updates(settings)[0]
            self.assertEqual(row["previous_version"], "")
            self.assertEqual(row["release_url"], "")
            self.assertEqual(row["release_notes"], "")

    def test_list_updates_respects_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            for i in range(5):
                update_history_store.record_update(settings, version=f"v0.1.{90 + i}")
            rows = update_history_store.list_updates(settings, limit=2)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["version"], "v0.1.94")

    def test_never_raises_when_storage_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(update_history_store, "_open", side_effect=RuntimeError("disk full")):
                result = update_history_store.record_update(settings, version="v0.1.99")
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
