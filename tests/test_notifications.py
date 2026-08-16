import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.notifications as notifications
from app.common.settings import Settings


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "notifications-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class RecordEventTests(unittest.TestCase):
    def test_records_a_real_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = notifications.record_event(settings, "torrent_completed", "Done", "My Game")
            self.assertEqual(result["event_type"], "torrent_completed")
            self.assertEqual(result["title"], "Done")

    def test_never_raises_when_storage_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(notifications._audit_store, "insert_event", side_effect=RuntimeError("disk full")):
                result = notifications.record_event(settings, "torrent_completed", "Done")
            self.assertIsNone(result)

    def test_event_types_tuple_has_sixteen_entries_matching_the_labels_map(self) -> None:
        self.assertEqual(len(notifications.EVENT_TYPES), 16)
        self.assertEqual(set(notifications.EVENT_TYPES), set(notifications.EVENT_TYPE_LABELS.keys()))


if __name__ == "__main__":
    unittest.main()
