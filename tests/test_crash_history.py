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
