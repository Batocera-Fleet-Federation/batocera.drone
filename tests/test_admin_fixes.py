import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.device import admin_fixes
from app.device.fix_assets import game_crash_notifier, lindbergh_input_guard, switch_gui_launcher
from app.drone_api import Settings
from app.web import handlers_system


GENERATOR_SOURCE = '''def generate(emulator, rom):
    commandArray = ["./"+emulator+".AppImage", "-f",  "-g", rom ]
    return commandArray
'''

LEGACY_GENERATOR_SOURCE = '''def generate(emulator, rom):
    commandArray = ["./"+emulator+".AppImage", "-f",  "-g", rom ]
    # >>> gui-autoload hotfix (smash): legacy
    if emulator in ('eden', 'citron') and '01006A800016E000' in str(rom).upper():
        commandArray = ["/bin/bash", "/userdata/system/rgs/generators/yuzu/gui-autoload.sh", emulator, str(rom)]
    # <<< gui-autoload hotfix
    return commandArray
'''


def build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "admin-fixes-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def write_generator(root: Path, source: str = GENERATOR_SOURCE) -> Path:
    path = root / "system" / "rgs" / "generators" / "yuzu" / "yuzuMainlineGenerator.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def write_switch_library(root: Path) -> None:
    switch = root / "roms" / "switch"
    switch.mkdir(parents=True, exist_ok=True)
    (switch / "Smash [01006A800016E000].xci").write_bytes(b"rom")
    (switch / "Mario.nsp").write_bytes(b"rom")
    (switch / "notes.txt").write_text("ignore", encoding="utf-8")
    (switch / "gamelist.xml").write_text(
        "<gameList><game><path>./Smash [01006A800016E000].xci</path>"
        "<name>Super Smash Bros. Ultimate</name></game></gameList>",
        encoding="utf-8",
    )


class AdminFixManagerTests(unittest.TestCase):
    def test_lindbergh_guard_enable_and_disable_are_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            enabled = admin_fixes.set_fix_enabled(settings, admin_fixes.LINDBERGH_INPUT_GUARD_ID, True)
            hook = Path(tmp) / "system" / "scripts" / admin_fixes.HOOK_FILENAME
            self.assertTrue(enabled["enabled"])
            self.assertTrue(enabled["managed"])
            self.assertTrue(hook.is_file())
            self.assertTrue(hook.stat().st_mode & 0o100)

            disabled = admin_fixes.set_fix_enabled(settings, admin_fixes.LINDBERGH_INPUT_GUARD_ID, False)
            self.assertFalse(disabled["enabled"])
            self.assertFalse(hook.exists())

    def test_crash_notifier_enable_and_disable_are_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            hook = Path(tmp) / "system" / "scripts" / admin_fixes.CRASH_NOTIFIER_HOOK_FILENAME
            self.assertEqual(admin_fixes.get_fix(settings, admin_fixes.CRASH_NOTIFIER_ID)["status"], "disabled")
            enabled = admin_fixes.set_fix_enabled(settings, admin_fixes.CRASH_NOTIFIER_ID, True)
            self.assertTrue(enabled["enabled"])
            self.assertTrue(enabled["managed"])
            self.assertTrue(hook.is_file())
            self.assertTrue(hook.stat().st_mode & 0o100)
            self.assertTrue((Path(tmp) / "system" / "game-crash-notifier" / "config.json").is_file())
            # The Lindbergh guard stays independent of this fix.
            self.assertFalse(admin_fixes.get_fix(settings, admin_fixes.LINDBERGH_INPUT_GUARD_ID)["enabled"])

            disabled = admin_fixes.set_fix_enabled(settings, admin_fixes.CRASH_NOTIFIER_ID, False)
            self.assertFalse(disabled["enabled"])
            self.assertFalse(hook.exists())

    def test_switch_library_uses_gamelist_names_and_supported_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = build_settings(root)
            write_switch_library(root)
            games = admin_fixes.list_switch_games(settings)
            self.assertEqual([game["name"] for game in games], ["Mario", "Super Smash Bros. Ultimate"])
            self.assertNotIn("notes.txt", [game["path"] for game in games])

    def test_switch_fix_migrates_legacy_smash_patch_and_keeps_dynamic_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = build_settings(root)
            generator = write_generator(root, LEGACY_GENERATOR_SOURCE)
            write_switch_library(root)
            smash = "Smash [01006A800016E000].xci"

            legacy_status = admin_fixes.get_fix(settings, admin_fixes.SWITCH_GUI_WORKAROUND_ID)
            self.assertTrue(legacy_status["enabled"])
            self.assertTrue(legacy_status["legacy_detected"])
            self.assertFalse(legacy_status["managed"])
            self.assertEqual(legacy_status["selected_games"], [smash])

            status = admin_fixes.set_fix_enabled(
                settings,
                admin_fixes.SWITCH_GUI_WORKAROUND_ID,
                True,
                scope="selected",
                selected_games=[smash],
            )
            source = generator.read_text(encoding="utf-8")
            config = json.loads((root / "system" / "switch-gui-workaround" / "config.json").read_text())
            self.assertTrue(status["enabled"])
            self.assertTrue(status["managed"])
            self.assertIn(admin_fixes.SWITCH_GENERATOR_MARKER_START, source)
            self.assertIn("'eden-legacy'", source)
            self.assertIn("'eden-pgo'", source)
            self.assertIn("'citron-legacy'", source)
            self.assertNotIn("# >>> gui-autoload hotfix", source)
            self.assertEqual(config["selected_games"], [smash])
            self.assertEqual(config["game_names"][smash], "Super Smash Bros. Ultimate")
            compile(source, str(generator), "exec")

            disabled = admin_fixes.set_fix_enabled(
                settings,
                admin_fixes.SWITCH_GUI_WORKAROUND_ID,
                False,
                scope="all",
                selected_games=[],
            )
            source = generator.read_text(encoding="utf-8")
            config = json.loads((root / "system" / "switch-gui-workaround" / "config.json").read_text())
            self.assertFalse(disabled["enabled"])
            self.assertNotIn(admin_fixes.SWITCH_GENERATOR_MARKER_START, source)
            self.assertEqual(config["scope"], "all")
            self.assertFalse(config["enabled"])

    def test_switch_disable_removes_the_legacy_patch_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = build_settings(root)
            generator = write_generator(root, LEGACY_GENERATOR_SOURCE)
            write_switch_library(root)
            admin_fixes.set_fix_enabled(
                settings,
                admin_fixes.SWITCH_GUI_WORKAROUND_ID,
                False,
                scope="selected",
                selected_games=["Smash [01006A800016E000].xci"],
            )
            source = generator.read_text(encoding="utf-8")
            self.assertNotIn("# >>> gui-autoload hotfix", source)
            self.assertIn('commandArray = ["./"+emulator+".AppImage", "-f",  "-g", rom ]', source)

    def test_switch_reenable_repairs_an_older_managed_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = build_settings(root)
            generator = write_generator(root)
            write_switch_library(root)
            smash = "Smash [01006A800016E000].xci"
            admin_fixes.set_fix_enabled(
                settings,
                admin_fixes.SWITCH_GUI_WORKAROUND_ID,
                True,
                scope="selected",
                selected_games=[smash],
            )
            old_source = generator.read_text(encoding="utf-8").replace(
                admin_fixes.SWITCH_EMULATOR_CONDITION,
                "if emulator in ('eden', 'citron'):",
            )
            generator.write_text(old_source, encoding="utf-8")
            self.assertEqual(admin_fixes.get_fix(settings, admin_fixes.SWITCH_GUI_WORKAROUND_ID)["status"], "modified")

            repaired = admin_fixes.set_fix_enabled(
                settings,
                admin_fixes.SWITCH_GUI_WORKAROUND_ID,
                True,
                scope="selected",
                selected_games=[smash],
            )
            self.assertTrue(repaired["managed"])
            self.assertIn(admin_fixes.SWITCH_EMULATOR_CONDITION, generator.read_text(encoding="utf-8"))

    def test_switch_fix_refuses_unknown_generator_without_modifying_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = build_settings(root)
            generator = write_generator(root, "def generate():\n    return []\n")
            before = generator.read_bytes()
            with self.assertRaisesRegex(OSError, "layout is not recognized"):
                admin_fixes.set_fix_enabled(settings, admin_fixes.SWITCH_GUI_WORKAROUND_ID, True)
            self.assertEqual(generator.read_bytes(), before)


class InstalledFixAssetTests(unittest.TestCase):
    def test_lindbergh_selection_protects_lightguns_and_single_controllers(self) -> None:
        groups = [
            {"usb_id": "1-1", "names": ["Sinden Lightgun", "Sinden Lightgun"], "joysticks": ["js0", "js1"]},
            {"usb_id": "1-2", "names": ["Nintendo GameCube Adapter"] * 4, "joysticks": ["js2", "js3", "js4", "js5"]},
            {"usb_id": "1-3", "names": ["DualShock 4"], "joysticks": ["js6"]},
        ]
        config = {
            "protected_name_patterns": ["sinden", "light[ -]?gun"],
            "candidate_name_patterns": ["gamecube", "nintendo.*adapter"],
        }
        selected = lindbergh_input_guard.select_groups_to_detach(groups, 4, config)
        self.assertEqual([group["usb_id"] for group in selected], ["1-2"])

    def test_crash_signature_is_detected_but_launcher_error_level_is_not(self) -> None:
        config = {"short_session_seconds": 15, "joystick_hint_threshold": 8}
        crashed = game_crash_notifier.assess_launch("...\n*** stack smashing detected ***: terminated\n", 40, "", config)
        self.assertTrue(crashed["crashed"])
        # The launcher logs all emulator stderr as ERROR on every exit.
        clean = game_crash_notifier.assess_launch("2026 ERROR (emulatorlauncher.py:604):runCommand [INFO] fine", 600, "", config)
        self.assertFalse(clean["crashed"])

    def test_hotkeygen_traceback_is_not_a_crash_but_launcher_traceback_is(self) -> None:
        config = {"short_session_seconds": 15, "joystick_hint_threshold": 8}
        noise = (
            "Traceback (most recent call last):\n"
            '  File "/usr/bin/hotkeygen", line 564, in <module>\n'
            "    do_new_context(new_context_name)\n"
            "ProcessLookupError: [Errno 3] No such process\n"
        )
        self.assertFalse(game_crash_notifier.assess_launch(noise + "[INFO] ok\n", 600, "", config)["crashed"])
        real = (
            "Traceback (most recent call last):\n"
            '  File "/usr/lib/python3/site-packages/configgen/emulatorlauncher.py", line 700, in <module>\n'
            "KeyError: 'core'\n"
        )
        self.assertTrue(game_crash_notifier.assess_launch(noise + real, 600, "", config)["crashed"])

    def test_short_session_needs_kernel_evidence(self) -> None:
        config = {"short_session_seconds": 15, "joystick_hint_threshold": 8}
        self.assertFalse(game_crash_notifier.assess_launch("", 3, "", config)["crashed"])
        killed = game_crash_notifier.assess_launch("", 3, "Out of memory: Killed process 5", config)
        self.assertTrue(killed["crashed"])
        self.assertFalse(game_crash_notifier.assess_launch("", 300, "Out of memory: Killed process 5", config)["crashed"])

    def test_kernel_evidence_respects_the_session_window(self) -> None:
        dmesg = "[   10.0] retroarch[1]: segfault at 0\n[  500.0] retroarch[2]: segfault at 0\n"
        self.assertIn("retroarch[2]", game_crash_notifier.kernel_evidence(dmesg, 495.0, 505.0))
        self.assertEqual(game_crash_notifier.kernel_evidence(dmesg, 100.0, 200.0), "")

    def test_messages_name_game_give_an_action_and_point_to_the_logs(self) -> None:
        config = {"short_session_seconds": 15, "joystick_hint_threshold": 8}
        verdict = game_crash_notifier.assess_launch("stack smashing detected", 5, "", config)
        names = ["Nintendo GameCube Adapter (x4)", "Sinden Lightgun (x2)"]
        first, second = game_crash_notifier.build_messages(
            "snes", "/userdata/roms/snes/Super Mario World.zip", verdict, 10, names, config, "batocera")
        self.assertIn("Super Mario World (snes) crashed", first)
        self.assertIn("10 controllers connected", second)
        self.assertIn("GameCube Adapter (x4)", second)
        self.assertIn("unplugging", second)
        self.assertIn("Admin > Debug > System Logs on batocera", second)

    def test_actions_are_cause_specific_and_skip_controller_hint_for_other_causes(self) -> None:
        config = {"short_session_seconds": 15, "joystick_hint_threshold": 8}
        missing = game_crash_notifier.assess_launch("Failed to load content", 5, "", config)
        _, action = game_crash_notifier.build_messages("snes", "x.zip", missing, 12, ["Pad"], config, "box")
        self.assertIn("ROM file and any required BIOS", action)
        self.assertNotIn("controllers", action)
        few = game_crash_notifier.assess_launch("stack smashing detected", 5, "", config)
        _, action = game_crash_notifier.build_messages("snes", "x.zip", few, 4, ["Pad"], config, "box")
        self.assertNotIn("controllers connected", action)
        oom = game_crash_notifier.assess_launch("", 3, "Out of memory: Killed process 5", config)
        _, action = game_crash_notifier.build_messages("snes", "x.zip", oom, 4, [], config, "box")
        self.assertIn("lighter core", action)

    def test_joystick_names_are_grouped_with_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for index, name in enumerate(["Pad  A", "Pad  A", "Gun"]):
                device = Path(tmp) / f"js{index}" / "device"
                device.mkdir(parents=True)
                (device / "name").write_text(name + "\n", encoding="utf-8")
            with mock.patch.object(game_crash_notifier, "SYS_INPUT_ROOT", Path(tmp)):
                self.assertEqual(game_crash_notifier.joystick_names(), ["Pad A (x2)", "Gun"])

    def test_notifications_send_in_order_and_stop_when_es_never_answers(self) -> None:
        sent = []
        with mock.patch.object(game_crash_notifier.time, "sleep"), \
             mock.patch.object(game_crash_notifier, "post_toast", side_effect=lambda m: sent.append(m) or True):
            self.assertEqual(game_crash_notifier.send_notifications(["one", "two"]), 2)
        self.assertEqual(sent, ["one", "two"])
        clock = iter(range(0, 1000, 20))
        with mock.patch.object(game_crash_notifier.time, "sleep"), \
             mock.patch.object(game_crash_notifier.time, "time", side_effect=lambda: next(clock)), \
             mock.patch.object(game_crash_notifier, "post_toast", return_value=False):
            self.assertEqual(game_crash_notifier.send_notifications(["one", "two"]), 0)

    def test_log_reader_handles_truncated_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stderr.log"
            path.write_text("old\nnew crash\n", encoding="utf-8")
            self.assertEqual(game_crash_notifier.read_new_text(path, 4), "new crash\n")
            self.assertEqual(game_crash_notifier.read_new_text(path, 9999), "old\nnew crash\n")

    def test_watch_sends_toast_only_for_crashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "stderr.log"
            log_path.write_text("fine\n*** stack smashing detected ***\n", encoding="utf-8")
            state = {"pid": 1, "start_time": None, "system": "snes", "rom": "Game.zip",
                     "started": 0, "start_uptime": None, "stderr_offset": 0}
            with mock.patch.object(game_crash_notifier, "LAUNCH_STDERR", log_path), \
                 mock.patch.object(game_crash_notifier, "LOG_PATH", Path(tmp) / "notifier.log"), \
                 mock.patch.object(game_crash_notifier, "launcher_is_alive", return_value=False), \
                 mock.patch.object(game_crash_notifier, "read_dmesg", return_value=""), \
                 mock.patch.object(game_crash_notifier, "joystick_count", return_value=2), \
                 mock.patch.object(game_crash_notifier, "send_notifications", return_value=2) as notify:
                game_crash_notifier.watch(state)
                notify.assert_called_once()
                self.assertIn("Game (snes) crashed", notify.call_args[0][0][0])
                log_path.write_text("all good\n", encoding="utf-8")
                notify.reset_mock()
                with mock.patch.object(game_crash_notifier.time, "time", return_value=10_000):
                    game_crash_notifier.watch(state)
                notify.assert_not_called()

    def test_switch_launcher_uses_gui_only_for_selected_games(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            switch_root = root / "roms" / "switch"
            switch_root.mkdir(parents=True)
            selected_rom = switch_root / "Smash.xci"
            other_rom = switch_root / "Mario.nsp"
            selected_rom.write_bytes(b"rom")
            other_rom.write_bytes(b"rom")
            config = root / "config.json"
            config.write_text(json.dumps({
                "enabled": True,
                "scope": "selected",
                "selected_games": ["Smash.xci"],
                "game_names": {"Smash.xci": "Super Smash Bros. Ultimate"},
            }), encoding="utf-8")
            wrapper = root / "wrapper.sh"
            wrapper.write_text("#!/bin/bash\n", encoding="utf-8")

            with mock.patch.object(switch_gui_launcher, "CONFIG", config), \
                 mock.patch.object(switch_gui_launcher, "SWITCH_ROOT", switch_root), \
                 mock.patch.object(switch_gui_launcher, "WRAPPER", wrapper), \
                 mock.patch.object(switch_gui_launcher, "EMU_ROOT", root), \
                 mock.patch.object(switch_gui_launcher.os, "execv", side_effect=RuntimeError) as execute:
                with self.assertRaises(RuntimeError):
                    switch_gui_launcher.main(["eden-legacy", str(selected_rom)])
                self.assertEqual(execute.call_args.args[0], "/bin/bash")
                self.assertEqual(execute.call_args.args[1][-1], "Super Smash Bros. Ultimate")

                execute.reset_mock()
                with self.assertRaises(RuntimeError):
                    switch_gui_launcher.main(["eden-legacy", str(other_rom)])
                self.assertEqual(execute.call_args.args[0], str(root / "eden-legacy.AppImage"))
                self.assertEqual(execute.call_args.args[1][-3:-1], ["-f", "-g"])


class _FakeHandler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.response = None

    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.response = (status_code, payload)


def build_handler(settings: Settings):
    class Handler(handlers_system.HandlersSystemMixin, _FakeHandler):
        pass

    return Handler(settings)


class AdminFixHandlerTests(unittest.TestCase):
    def test_update_validates_boolean_and_unknown_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = build_handler(build_settings(Path(tmp)))
            handler._handle_admin_fix_update("missing", {"enabled": "yes"})
            self.assertEqual(handler.response[0], 400)
            handler._handle_admin_fix_update("missing", {"enabled": True})
            self.assertEqual(handler.response[0], 404)

    def test_list_returns_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = build_handler(build_settings(Path(tmp)))
            handler._handle_admin_fixes_list()
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertEqual({fix["id"] for fix in payload["fixes"]}, {
                admin_fixes.LINDBERGH_INPUT_GUARD_ID,
                admin_fixes.CRASH_NOTIFIER_ID,
                admin_fixes.SWITCH_GUI_WORKAROUND_ID,
            })


class AdminFixUiContractTests(unittest.TestCase):
    def test_admin_fixes_route_panel_modal_and_switch_selection_are_wired(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")
        self.assertIn('setHash(\'#admin/fixes\')', source)
        self.assertIn('async function renderAdminFixesPage()', source)
        self.assertIn('function showAdminFixDetails(fixId)', source)
        self.assertIn('async function repairAdminFix(fixId)', source)
        self.assertIn('value="all"', source)
        self.assertIn('class="form-check-input switch-fix-game"', source)
