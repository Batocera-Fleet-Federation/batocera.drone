import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.storage.audit_store as audit_store
from app.common.settings import Settings


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "audit-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class InsertEventTests(unittest.TestCase):
    def test_insert_event_writes_linked_audit_and_notification_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            notification = audit_store.insert_event(settings, "torrent_completed", "Torrent done", "My Game")
            self.assertEqual(notification["event_type"], "torrent_completed")
            self.assertEqual(notification["title"], "Torrent done")
            self.assertEqual(notification["message"], "My Game")
            self.assertFalse(notification["read"])
            self.assertIsNone(notification["read_at"])

            with audit_store._open(settings.userdata_root) as connection:
                audit_row = connection.execute("SELECT event_type, emailed_at FROM audit_log").fetchone()
                notif_row = connection.execute("SELECT audit_log_id, read_at FROM notifications").fetchone()
            self.assertEqual(audit_row[0], "torrent_completed")
            self.assertIsNone(audit_row[1])
            self.assertEqual(notif_row[0], notification["audit_log_id"])
            self.assertIsNone(notif_row[1])

    def test_insert_event_requires_event_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                audit_store.insert_event(settings, "", "title")

    def test_insert_event_serializes_details_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            audit_store.insert_event(settings, "asset_downloaded", "t", details={"asset_type": "rom", "n": 3})
            with audit_store._open(settings.userdata_root) as connection:
                row = connection.execute("SELECT details FROM audit_log").fetchone()
            self.assertIn('"asset_type": "rom"', row[0])

    def test_relayed_event_is_idempotent_by_source_and_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            first = audit_store.insert_relayed_event(
                settings,
                "asset_downloaded",
                "Asset downloaded",
                source_drone_id="satellite-1",
                source_event_id="42",
                message="Zelda.zip",
                details={"source_drone_name": "Living Room"},
                created_at="2026-08-14T12:00:00+00:00",
            )
            duplicate = audit_store.insert_relayed_event(
                settings,
                "asset_downloaded",
                "Asset downloaded again",
                source_drone_id="satellite-1",
                source_event_id="42",
            )
            self.assertFalse(first["duplicate"])
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(first["audit_log_id"], duplicate["audit_log_id"])
            with audit_store._open(settings.userdata_root) as connection:
                audit_count = connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
                notification_count = connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
                created_at = connection.execute("SELECT created_at FROM audit_log").fetchone()[0]
            self.assertEqual(audit_count, 1)
            self.assertEqual(notification_count, 1)
            self.assertEqual(created_at, "2026-08-14T12:00:00+00:00")


class DigestQueryTests(unittest.TestCase):
    def test_list_unsent_events_filters_by_type_and_excludes_emailed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            first = audit_store.insert_event(settings, "vpn_connected", "a")
            audit_store.insert_event(settings, "vpn_disconnected", "b")
            audit_store.insert_event(settings, "torrent_completed", "c")
            audit_store.mark_events_emailed(settings, [first["audit_log_id"]])

            unsent = audit_store.list_unsent_events(settings, ["vpn_connected", "vpn_disconnected"])
            self.assertEqual([item["event_type"] for item in unsent], ["vpn_disconnected"])

            unsent_torrents_only = audit_store.list_unsent_events(settings, ["torrent_completed"])
            self.assertEqual(len(unsent_torrents_only), 1)

    def test_list_unsent_events_with_no_types_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            audit_store.insert_event(settings, "vpn_connected", "a")
            self.assertEqual(audit_store.list_unsent_events(settings, []), [])

    def test_mark_events_emailed_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            event = audit_store.insert_event(settings, "vpn_connected", "a")
            first = audit_store.mark_events_emailed(settings, [event["audit_log_id"]])
            second = audit_store.mark_events_emailed(settings, [event["audit_log_id"]])
            self.assertEqual(first, 1)
            self.assertEqual(second, 0)  # already emailed -- WHERE emailed_at IS NULL excludes it


class NotificationsInboxTests(unittest.TestCase):
    def test_list_notifications_page_orders_newest_first_and_paginates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            created = [audit_store.insert_event(settings, "vpn_connected", f"item {i}") for i in range(5)]

            page = audit_store.list_notifications_page(settings, limit=2)
            self.assertEqual(len(page["items"]), 2)
            self.assertTrue(page["has_more"])
            self.assertEqual(page["items"][0]["id"], created[-1]["id"])  # newest first
            self.assertEqual(page["unread_count"], 5)

            next_page = audit_store.list_notifications_page(settings, before_id=page["next_before_id"], limit=2)
            self.assertEqual(len(next_page["items"]), 2)
            self.assertNotEqual(next_page["items"][0]["id"], page["items"][0]["id"])

    def test_unread_only_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            first = audit_store.insert_event(settings, "vpn_connected", "a")
            audit_store.insert_event(settings, "vpn_connected", "b")
            audit_store.mark_notification_read(settings, first["id"])

            unread = audit_store.list_notifications_page(settings, unread_only=True)
            self.assertEqual(len(unread["items"]), 1)
            self.assertEqual(unread["items"][0]["title"], "b")

    def test_mark_notification_read_returns_false_for_unknown_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertFalse(audit_store.mark_notification_read(settings, 999))

    def test_mark_all_notifications_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            for i in range(3):
                audit_store.insert_event(settings, "vpn_connected", f"item {i}")
            count = audit_store.mark_all_notifications_read(settings)
            self.assertEqual(count, 3)
            self.assertEqual(audit_store.unread_notification_count(settings), 0)

    def test_delete_notification_does_not_touch_audit_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            event = audit_store.insert_event(settings, "vpn_connected", "a")
            self.assertTrue(audit_store.delete_notification(settings, event["id"]))
            self.assertEqual(audit_store.list_notifications_page(settings)["items"], [])
            # The permanent audit trail (used by the email digest) must survive
            # a UI "clear" -- clearing notifications is a distinct lifecycle.
            unsent = audit_store.list_unsent_events(settings, ["vpn_connected"])
            self.assertEqual(len(unsent), 1)

    def test_clear_notifications_only_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            read_one = audit_store.insert_event(settings, "vpn_connected", "read")
            audit_store.insert_event(settings, "vpn_connected", "unread")
            audit_store.mark_notification_read(settings, read_one["id"])

            cleared = audit_store.clear_notifications(settings, only_read=True)
            self.assertEqual(cleared, 1)
            remaining = audit_store.list_notifications_page(settings)["items"]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["title"], "unread")

    def test_clear_notifications_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            audit_store.insert_event(settings, "vpn_connected", "a")
            audit_store.insert_event(settings, "vpn_connected", "b")
            cleared = audit_store.clear_notifications(settings)
            self.assertEqual(cleared, 2)
            self.assertEqual(audit_store.list_notifications_page(settings)["items"], [])


class PruneOldEventsTests(unittest.TestCase):
    def test_prune_never_touches_unsent_or_unread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            audit_store.insert_event(settings, "vpn_connected", "a")
            result = audit_store.prune_old_events(settings)
            self.assertEqual(result["audit_rows_pruned"], 0)
            self.assertEqual(result["notifications_pruned"], 0)
            self.assertEqual(len(audit_store.list_unsent_events(settings, ["vpn_connected"])), 1)

    def test_prune_removes_old_emailed_audit_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            event = audit_store.insert_event(settings, "vpn_connected", "old")
            audit_store.mark_events_emailed(settings, [event["audit_log_id"]])
            with audit_store._open(settings.userdata_root) as connection:
                connection.execute(
                    "UPDATE audit_log SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                    (event["audit_log_id"],),
                )
            result = audit_store.prune_old_events(settings)
            self.assertEqual(result["audit_rows_pruned"], 1)

    def test_prune_removes_old_read_notifications_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            old_read = audit_store.insert_event(settings, "vpn_connected", "old-read")
            audit_store.mark_notification_read(settings, old_read["id"])
            audit_store.insert_event(settings, "vpn_connected", "old-unread")
            with audit_store._open(settings.userdata_root) as connection:
                connection.execute("UPDATE notifications SET created_at = '2000-01-01T00:00:00+00:00'")
            result = audit_store.prune_old_events(settings)
            self.assertEqual(result["notifications_pruned"], 1)
            remaining_titles = [item["title"] for item in audit_store.list_notifications_page(settings)["items"]]
            self.assertEqual(remaining_titles, ["old-unread"])


class SchemaTests(unittest.TestCase):
    def test_indexes_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with audit_store._open(settings.userdata_root) as connection:
                audit_indexes = {row[1] for row in connection.execute("PRAGMA index_list(audit_log)")}
                notification_indexes = {row[1] for row in connection.execute("PRAGMA index_list(notifications)")}
            self.assertIn("idx_audit_log_created_at", audit_indexes)
            self.assertIn("idx_audit_log_pending_email", audit_indexes)
            self.assertIn("idx_notifications_created_at", notification_indexes)
            self.assertIn("idx_notifications_read_at", notification_indexes)
            self.assertIn("idx_notifications_audit_log_id", notification_indexes)


if __name__ == "__main__":
    unittest.main()
