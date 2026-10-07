import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.device import admin_fixes, crash_history
from app.device.fix_assets import game_crash_notifier
from app.web import handlers_diagnostics, openapi_spec
from tests.test_admin_fixes import build_settings


def write_history(root: Path, records: list, extra_lines: tuple = ()) -> Path:
    path = root / "system" / "game-crash-notifier" / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record) for record in records] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class CrashHistoryStoreTests(unittest.TestCase):
    def test_lists_newest_first_and_skips_corrupt_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            write_history(
                Path(tmp),
                [
                    {"time": "a", "epoch": 100, "game": "Old", "system": "snes", "reason": "r", "action": "a"},
                    {"time": "b", "epoch": 200, "game": "New", "system": "nes", "reason": "r", "action": "a",
                     "joysticks": ["Pad (x2)"], "rom_exists": False, "duration_seconds": 4},
                ],
                extra_lines=("{not json", "[1, 2]"),
            )
            result = crash_history.list_crashes(settings)
            self.assertEqual([row["game"] for row in result["crashes"]], ["New", "Old"])
            self.assertEqual(result["total"], 2)
            self.assertFalse(result["fix_enabled"])
            newest = result["crashes"][0]
            self.assertEqual(newest["joysticks"], ["Pad (x2)"])
            self.assertIs(newest["rom_exists"], False)
            self.assertIsNone(result["crashes"][1]["rom_exists"])

    def test_missing_history_is_empty_and_reports_fix_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            self.assertEqual(crash_history.list_crashes(settings)["crashes"], [])
            admin_fixes.set_fix_enabled(settings, admin_fixes.CRASH_NOTIFIER_ID, True)
            self.assertTrue(crash_history.list_crashes(settings)["fix_enabled"])

    def test_clear_removes_history_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            path = write_history(Path(tmp), [{"time": "a", "epoch": 1, "game": "G", "system": "s"}])
            self.assertEqual(crash_history.clear_crashes(settings), 1)
            self.assertFalse(path.exists())
            self.assertEqual(crash_history.clear_crashes(settings), 0)


class _FakeHandler:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.response = None

    def _send_json(self, status_code, payload, cache_key=None, extra_headers=None) -> None:
        self.response = (status_code, payload)


class CrashHistoryHandlerTests(unittest.TestCase):
    def test_get_and_clear_routes(self) -> None:
        class Handler(handlers_diagnostics.HandlersDiagnosticsMixin, _FakeHandler):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            handler = Handler(build_settings(Path(tmp)))
            write_history(Path(tmp), [{"time": "a", "epoch": 1, "game": "G", "system": "snes", "reason": "r"}])
            handler._handle_admin_crash_history()
            self.assertEqual(handler.response[0], 200)
            self.assertEqual(handler.response[1]["crashes"][0]["game"], "G")
            handler._handle_admin_crash_history_clear()
            self.assertEqual(handler.response, (200, {"cleared": 1}))
            handler._handle_admin_crash_history()
            self.assertEqual(handler.response[1]["crashes"], [])


class CrashRecordTests(unittest.TestCase):
    def test_record_carries_the_evidence_needed_to_fix_the_crash(self) -> None:
        config = {"short_session_seconds": 15, "joystick_hint_threshold": 8}
        log_text = "\n".join([f"line {i}" for i in range(100)] + ["*** stack smashing detected ***", "after"])
        verdict = game_crash_notifier.assess_launch(log_text, 5, "", config)
        with tempfile.TemporaryDirectory() as tmp:
            rom = Path(tmp) / "Super Mario World.zip"
            rom.write_bytes(b"x" * 10)
            state = {"system": "snes", "rom": str(rom), "emulator": "libretro", "core": "snes9x"}
            record = game_crash_notifier.build_record(
                state, verdict, 5.2, log_text, "", ["first", "second"], 10, ["Pad (x4)"])
        self.assertEqual(record["game"], "Super Mario World")
        self.assertTrue(record["rom_exists"])
        self.assertEqual(record["rom_size_bytes"], 10)
        self.assertEqual((record["emulator"], record["core"]), ("libretro", "snes9x"))
        self.assertEqual(record["joystick_count"], 10)
        self.assertEqual(record["duration_seconds"], 5)
        self.assertIn("stack smashing detected", record["log_excerpt"])
        self.assertNotIn("line 0\n", record["log_excerpt"])
        self.assertEqual(record["toasts"], ["first", "second"])

    def test_missing_rom_is_reported(self) -> None:
        verdict = {"reason": "r", "action": "a", "signature": "x", "short": False}
        record = game_crash_notifier.build_record(
            {"system": "snes", "rom": "/nope/Game.zip"}, verdict, 1, "", "", [], 0, [])
        self.assertFalse(record["rom_exists"])
        self.assertIsNone(record["rom_size_bytes"])

    def test_history_is_capped_and_round_trips_to_the_drone_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            path = Path(tmp) / "system" / "game-crash-notifier" / "history.jsonl"
            with mock.patch.object(game_crash_notifier, "HISTORY_PATH", path), \
                 mock.patch.object(game_crash_notifier, "HISTORY_LIMIT", 3):
                for index in range(5):
                    game_crash_notifier.write_history({"time": "t", "epoch": index, "game": f"G{index}", "system": "s"})
            result = crash_history.list_crashes(settings)
            self.assertEqual([row["game"] for row in result["crashes"]], ["G4", "G3", "G2"])


class CrashHistoryWiringTests(unittest.TestCase):
    def test_routes_ui_and_spec_are_wired(self) -> None:
        root = Path(__file__).resolve().parents[1]
        routes = root.joinpath("app/web/api_routes.py").read_text(encoding="utf-8")
        self.assertIn('parts[1] == "crash-history"', routes)
        self.assertIn("_handle_admin_crash_history_clear", routes)
        script = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")
        self.assertIn('["crashes", "Game Crashes"', script)
        self.assertIn("async function renderGameCrashesPage()", script)
        self.assertIn('hash === "#admin/crashes"', script)
        self.assertIn("themed-table", script[script.index("async function renderGameCrashesPage()"):])
        spec = openapi_spec.build_openapi_spec("test")
        self.assertIn("/admin/crash-history", spec["paths"])
        self.assertIn("/admin/crash-history/clear", spec["paths"])
        self.assertIn("CrashHistoryEntry", spec["components"]["schemas"])


class CrashEmailTests(unittest.TestCase):
    RECORD = {
        "time": "2026-10-07T06:39:34-0500", "epoch": 2_000_000_000, "game": "Super Mario World", "system": "snes",
        "reason": "the emulator aborted with a memory-corruption error", "action": "Try unplugging USB controllers.",
        "rom_path": "/userdata/roms/snes/Super Mario World.zip", "rom_exists": True, "rom_size_bytes": 337766,
        "emulator": "libretro", "core": "snes9x", "signature": "stack smashing detected", "duration_seconds": 17,
        "joystick_count": 10, "joysticks": ["GameCube Adapter (x4)"], "memory_available_mb": 25938,
        "batocera_version": "43.1", "hostname": "BATOCERA", "log_excerpt": "line a\n*** stack smashing detected ***",
        "kernel_evidence": "retroarch[1]: segfault at 0",
    }

    def _smtp_settings(self, tmp):
        from app.device import smtp_manager
        settings = build_settings(Path(tmp))
        smtp_manager.update_settings(settings, {
            "host": "smtp.example.com", "port": 587, "use_starttls": True, "use_ssl": False,
            "username": "me@example.com", "password": "pw", "from_address": "d@example.com",
            "recipient_email": "o@example.com",
        })
        return smtp_manager, settings

    def test_report_contains_everything_the_debug_page_shows(self) -> None:
        report = crash_history.format_crash_report({**self.RECORD, "device_id": "drone-1"})
        for expected in ("BATOCERA (drone-1)", "Super Mario World", "snes", "memory-corruption", "Try unplugging",
                         "/userdata/roms/snes/Super Mario World.zip", "337,766 bytes", "libretro / snes9x",
                         "stack smashing detected", "17s", "10 (GameCube Adapter (x4))", "25938 MB", "43.1",
                         "segfault at 0", "Launch log around the failure"):
            self.assertIn(expected, report)

    def test_first_pass_skips_old_history_but_keeps_a_just_happened_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            now = 2_000_000_000
            write_history(Path(tmp), [
                {**self.RECORD, "epoch": now - 10_000, "game": "Old"},
                {**self.RECORD, "epoch": now - 5, "game": "Fresh"},
            ])
            self.assertEqual(crash_history.ingest_new_crashes(settings, now=now), 1)
            self.assertEqual(crash_history.ingest_new_crashes(settings, now=now), 0)
            from app.storage import audit_store
            events = audit_store.list_unsent_events(settings, ["game_crash"], limit=10)
            self.assertEqual([event["title"] for event in events], ["Fresh (snes) crashed"])
            self.assertEqual(events[0]["details"]["device_id"], settings.device_id)

    def test_later_crashes_are_recorded_once_each(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            now = 2_000_000_000
            write_history(Path(tmp), [])
            self.assertEqual(crash_history.ingest_new_crashes(settings, now=now), 0)
            write_history(Path(tmp), [{**self.RECORD, "epoch": now + 1}])
            self.assertEqual(crash_history.ingest_new_crashes(settings, now=now + 2), 1)
            write_history(Path(tmp), [{**self.RECORD, "epoch": now + 1}, {**self.RECORD, "epoch": now + 9, "game": "Two"}])
            self.assertEqual(crash_history.ingest_new_crashes(settings, now=now + 10), 1)

    def test_crash_email_is_queued_immediately_with_full_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            smtp_manager, settings = self._smtp_settings(tmp)
            # An older digest was just attempted, so the interval has NOT elapsed.
            smtp_manager._notifications.record_event(settings, "torrent_completed", "Earlier")
            smtp_manager._save_state(settings, last_digest_attempt_at=smtp_manager._now_iso())
            self.assertEqual(smtp_manager.send_digest_if_needed(settings)["status"], "skipped")
            write_history(Path(tmp), [])
            crash_history.ingest_new_crashes(settings, now=self.RECORD["epoch"])
            write_history(Path(tmp), [self.RECORD])
            crash_history.ingest_new_crashes(settings, now=self.RECORD["epoch"] + 1)
            result = smtp_manager.send_digest_if_needed(settings)
            self.assertEqual(result["status"], "queued")
            job = smtp_manager._mail_store.pending(settings)[0]
            self.assertIn("Super Mario World (snes) crashed", job["subject"])
            self.assertIn("(+1 more)", job["subject"])
            self.assertIn("stack smashing detected", job["body"])
            self.assertIn("GameCube Adapter (x4)", job["body"])
            self.assertIn("Drone:", job["body"])

    def test_toggle_off_keeps_the_event_in_the_inbox_but_sends_no_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            smtp_manager, settings = self._smtp_settings(tmp)
            self.assertTrue(smtp_manager._load_state(settings)["notify"]["game_crash"])
            smtp_manager.update_notification_toggles(settings, {"game_crash": False})
            write_history(Path(tmp), [])
            crash_history.ingest_new_crashes(settings, now=self.RECORD["epoch"])
            write_history(Path(tmp), [self.RECORD])
            crash_history.ingest_new_crashes(settings, now=self.RECORD["epoch"] + 1)
            self.assertEqual(smtp_manager.send_digest_if_needed(settings)["status"], "skipped")
            self.assertEqual(smtp_manager._mail_store.pending(settings), [])
            from app.storage import audit_store
            self.assertEqual(len(audit_store.list_unsent_events(settings, ["game_crash"], limit=5)), 1)
