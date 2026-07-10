import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
import app.device.automation as automation
import app.overmind.actions as overmind_actions
from app.drone_api import (
    RomRepository,
    Settings,
    _execute_overmind_action,
    _load_automation_config,
    _normalize_idle_game_exit_config,
    _normalize_idle_volume_config,
    _push_automation_config_to_overmind,
    _read_last_input_activity,
    _run_idle_game_exit_automation_once,
    _run_idle_volume_automation_once,
    _save_automation_config,
)


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "OVERMIND_DEVICE_ID": "local-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class IdleVolumeConfigTests(unittest.TestCase):
    def test_defaults_are_disabled_with_5_minutes_and_25_percent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            config = _load_automation_config(settings)["idle_volume"]
            self.assertFalse(config["enabled"])
            self.assertEqual(config["idle_minutes"], 5)
            self.assertEqual(config["target_volume"], 25)

    def test_normalize_clamps_and_coerces(self) -> None:
        normalized = _normalize_idle_volume_config(
            {"enabled": "yes", "idle_minutes": "0", "target_volume": "250"}
        )
        self.assertTrue(normalized["enabled"])
        self.assertEqual(normalized["idle_minutes"], 1)
        self.assertEqual(normalized["target_volume"], 100)

    def test_normalize_handles_garbage_values(self) -> None:
        normalized = _normalize_idle_volume_config(
            {"idle_minutes": "abc", "target_volume": None}
        )
        self.assertEqual(normalized["idle_minutes"], 5)
        self.assertEqual(normalized["target_volume"], 25)

    def test_save_round_trips_and_normalizes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _save_automation_config(
                settings,
                {"idle_volume": {"enabled": True, "idle_minutes": 9999, "target_volume": -5}},
            )
            config = _load_automation_config(settings)["idle_volume"]
            self.assertTrue(config["enabled"])
            self.assertEqual(config["idle_minutes"], 1440)
            self.assertEqual(config["target_volume"], 0)


class IdleGameExitConfigTests(unittest.TestCase):
    def test_defaults_are_disabled_with_15_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            config = _load_automation_config(settings)["idle_game_exit"]
            self.assertFalse(config["enabled"])
            self.assertEqual(config["idle_minutes"], 15)

    def test_normalize_clamps_and_coerces(self) -> None:
        normalized = _normalize_idle_game_exit_config({"enabled": "yes", "idle_minutes": "0"})
        self.assertTrue(normalized["enabled"])
        self.assertEqual(normalized["idle_minutes"], 1)

    def test_normalize_handles_garbage_values(self) -> None:
        normalized = _normalize_idle_game_exit_config({"idle_minutes": "abc"})
        self.assertEqual(normalized["idle_minutes"], 15)

    def test_save_round_trips_and_normalizes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _save_automation_config(
                settings,
                {"idle_game_exit": {"enabled": True, "idle_minutes": 9999}},
            )
            config = _load_automation_config(settings)["idle_game_exit"]
            self.assertTrue(config["enabled"])
            self.assertEqual(config["idle_minutes"], 1440)

    def test_saving_one_section_preserves_the_other(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _save_automation_config(
                settings,
                {"idle_volume": {"enabled": True, "idle_minutes": 7, "target_volume": 15}},
            )
            _save_automation_config(
                settings,
                {"idle_game_exit": {"enabled": True, "idle_minutes": 20}},
            )
            config = _load_automation_config(settings)
            # Saving idle_game_exit must not reset the idle_volume section back to defaults.
            self.assertEqual(
                config["idle_volume"],
                {"enabled": True, "idle_minutes": 7, "target_volume": 15},
            )
            self.assertEqual(
                config["idle_game_exit"],
                {"enabled": True, "idle_minutes": 20},
            )


class InputActivityFileTests(unittest.TestCase):
    def test_read_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                "os.environ",
                {"DRONE_INPUT_ACTIVITY_FILE": str(Path(tmp) / "missing")},
                clear=False,
            ):
                self.assertIsNone(_read_last_input_activity())

    def test_read_parses_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last-input-activity"
            path.write_text("1700000000\n", encoding="utf-8")
            with mock.patch.dict(
                "os.environ", {"DRONE_INPUT_ACTIVITY_FILE": str(path)}, clear=False
            ):
                self.assertEqual(_read_last_input_activity(), 1700000000.0)

    def test_read_blank_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last-input-activity"
            path.write_text("   \n", encoding="utf-8")
            with mock.patch.dict(
                "os.environ", {"DRONE_INPUT_ACTIVITY_FILE": str(path)}, clear=False
            ):
                self.assertIsNone(_read_last_input_activity())


class IdleVolumeAutomationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        # The runner uses module-level arming state; reset it per test.
        drone_api._IDLE_VOLUME_LAST_ARMED_ACTIVITY = None

    def _enable(self, settings: Settings, *, idle_minutes: int = 5, target: int = 25) -> None:
        _save_automation_config(
            settings,
            {"idle_volume": {"enabled": True, "idle_minutes": idle_minutes, "target_volume": target}},
        )

    def test_disabled_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(automation, "_apply_audio_volume") as apply_mock:
                _run_idle_volume_automation_once(settings)
                apply_mock.assert_not_called()

    def test_no_monitor_data_does_not_lower(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings)
            with mock.patch.object(automation, "_read_last_input_activity", return_value=None), \
                 mock.patch.object(automation, "_apply_audio_volume") as apply_mock:
                _run_idle_volume_automation_once(settings)
                apply_mock.assert_not_called()

    def test_recent_input_does_not_lower(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings, idle_minutes=5)
            recent = time.time() - 60  # 1 minute idle, threshold is 5
            with mock.patch.object(automation, "_read_last_input_activity", return_value=recent), \
                 mock.patch.object(automation, "_get_audio_volume", return_value=80), \
                 mock.patch.object(automation, "_apply_audio_volume") as apply_mock:
                _run_idle_volume_automation_once(settings)
                apply_mock.assert_not_called()

    def test_active_game_process_does_not_lower(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings, idle_minutes=5, target=25)
            idle = time.time() - 600
            with mock.patch.object(automation, "_find_running_emulatorlauncher", return_value={"pid": 123, "rom_path": "/userdata/roms/snes/game.sfc"}), \
                 mock.patch.object(automation, "_read_last_input_activity", return_value=idle), \
                 mock.patch.object(automation, "_apply_audio_volume") as apply_mock:
                _run_idle_volume_automation_once(settings)
                apply_mock.assert_not_called()

    def test_idle_past_threshold_lowers_once_then_holds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings, idle_minutes=5, target=25)
            idle = time.time() - 600  # 10 minutes idle
            with mock.patch.object(automation, "_read_last_input_activity", return_value=idle), \
                 mock.patch.object(automation, "_get_audio_volume", return_value=80), \
                 mock.patch.object(automation, "_apply_audio_volume", return_value=25) as apply_mock:
                _run_idle_volume_automation_once(settings)
                _run_idle_volume_automation_once(settings)  # still idle, same activity stamp
                apply_mock.assert_called_once_with(settings, 25)

    def test_new_input_rearms_and_lowers_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings, idle_minutes=5, target=25)
            first_idle = time.time() - 600
            with mock.patch.object(automation, "_get_audio_volume", return_value=80), \
                 mock.patch.object(automation, "_apply_audio_volume", return_value=25) as apply_mock:
                with mock.patch.object(automation, "_read_last_input_activity", return_value=first_idle):
                    _run_idle_volume_automation_once(settings)
                # Fresh input arrives, then the device goes idle again.
                second_idle = time.time() - 600 + 1
                with mock.patch.object(automation, "_read_last_input_activity", return_value=second_idle):
                    _run_idle_volume_automation_once(settings)
                self.assertEqual(apply_mock.call_count, 2)

    def test_already_below_target_does_not_set_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings, idle_minutes=5, target=25)
            idle = time.time() - 600
            with mock.patch.object(automation, "_read_last_input_activity", return_value=idle), \
                 mock.patch.object(automation, "_get_audio_volume", return_value=10), \
                 mock.patch.object(automation, "_apply_audio_volume") as apply_mock:
                _run_idle_volume_automation_once(settings)
                apply_mock.assert_not_called()


_RUNNING_GAME = {"pid": 123, "rom_path": "/userdata/roms/snes/game.sfc"}


class IdleGameExitAutomationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        # The runner uses module-level arming state; reset it per test.
        drone_api._IDLE_GAME_EXIT_LAST_ARMED_ACTIVITY = None

    def _enable(self, settings: Settings, *, idle_minutes: int = 15) -> None:
        _save_automation_config(
            settings,
            {"idle_game_exit": {"enabled": True, "idle_minutes": idle_minutes}},
        )

    def test_disabled_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(automation, "_kill_running_emulator") as kill_mock:
                _run_idle_game_exit_automation_once(settings)
                kill_mock.assert_not_called()

    def test_no_game_running_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings, idle_minutes=5)
            idle = time.time() - 600
            with mock.patch.object(automation, "_find_running_emulatorlauncher", return_value=None), \
                 mock.patch.object(automation, "_read_last_input_activity", return_value=idle), \
                 mock.patch.object(automation, "_kill_running_emulator") as kill_mock:
                _run_idle_game_exit_automation_once(settings)
                kill_mock.assert_not_called()

    def test_no_monitor_data_does_not_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings)
            with mock.patch.object(automation, "_find_running_emulatorlauncher", return_value=_RUNNING_GAME), \
                 mock.patch.object(automation, "_read_last_input_activity", return_value=None), \
                 mock.patch.object(automation, "_kill_running_emulator") as kill_mock:
                _run_idle_game_exit_automation_once(settings)
                kill_mock.assert_not_called()

    def test_recent_input_does_not_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings, idle_minutes=5)
            recent = time.time() - 60  # 1 minute idle, threshold is 5
            with mock.patch.object(automation, "_find_running_emulatorlauncher", return_value=_RUNNING_GAME), \
                 mock.patch.object(automation, "_read_last_input_activity", return_value=recent), \
                 mock.patch.object(automation, "_kill_running_emulator") as kill_mock:
                _run_idle_game_exit_automation_once(settings)
                kill_mock.assert_not_called()

    def test_idle_past_threshold_exits_once_then_holds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings, idle_minutes=5)
            idle = time.time() - 600  # 10 minutes idle
            with mock.patch.object(automation, "_find_running_emulatorlauncher", return_value=_RUNNING_GAME), \
                 mock.patch.object(automation, "_read_last_input_activity", return_value=idle), \
                 mock.patch.object(automation, "_kill_running_emulator") as kill_mock:
                _run_idle_game_exit_automation_once(settings)
                _run_idle_game_exit_automation_once(settings)  # still idle, same activity stamp
                kill_mock.assert_called_once()

    def test_new_input_rearms_and_exits_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings, idle_minutes=5)
            first_idle = time.time() - 600
            with mock.patch.object(automation, "_find_running_emulatorlauncher", return_value=_RUNNING_GAME), \
                 mock.patch.object(automation, "_kill_running_emulator") as kill_mock:
                with mock.patch.object(automation, "_read_last_input_activity", return_value=first_idle):
                    _run_idle_game_exit_automation_once(settings)
                # Fresh input arrives (e.g. a new game is launched), then idle again.
                second_idle = time.time() - 600 + 1
                with mock.patch.object(automation, "_read_last_input_activity", return_value=second_idle):
                    _run_idle_game_exit_automation_once(settings)
                self.assertEqual(kill_mock.call_count, 2)

    def test_kill_failure_does_not_arm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings, idle_minutes=5)
            idle = time.time() - 600
            with mock.patch.object(automation, "_find_running_emulatorlauncher", return_value=_RUNNING_GAME), \
                 mock.patch.object(automation, "_read_last_input_activity", return_value=idle), \
                 mock.patch.object(automation, "_kill_running_emulator", side_effect=OSError("boom")) as kill_mock:
                _run_idle_game_exit_automation_once(settings)
                _run_idle_game_exit_automation_once(settings)
                self.assertEqual(kill_mock.call_count, 2)


class IdleVolumeOvermindActionTests(unittest.TestCase):
    def setUp(self) -> None:
        drone_api._IDLE_VOLUME_LAST_ARMED_ACTIVITY = None

    def test_action_saves_config_and_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            repo = RomRepository(Path(tmp) / "roms", Path(tmp) / "bios")
            status, message, result = _execute_overmind_action(
                settings,
                repo,
                {
                    "action": "set_idle_volume_automation",
                    "payload": {"enabled": True, "idle_minutes": 12, "target_volume": 10},
                },
            )
            self.assertEqual(status, "completed")
            self.assertEqual(result["type"], "idle_volume_automation")
            self.assertEqual(result["enabled"], True)
            self.assertEqual(result["idle_minutes"], 12)
            self.assertEqual(result["target_volume"], 10)
            self.assertIn("enabled", message)
            stored = _load_automation_config(settings)["idle_volume"]
            self.assertEqual(stored, {"enabled": True, "idle_minutes": 12, "target_volume": 10})

    def test_action_partial_payload_merges_and_clamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            repo = RomRepository(Path(tmp) / "roms", Path(tmp) / "bios")
            _save_automation_config(
                settings,
                {"idle_volume": {"enabled": True, "idle_minutes": 5, "target_volume": 25}},
            )
            status, _message, result = _execute_overmind_action(
                settings, repo, {"action": "set_idle_volume_automation", "payload": {"target_volume": 999}}
            )
            self.assertEqual(status, "completed")
            self.assertEqual(result["enabled"], True)  # preserved
            self.assertEqual(result["idle_minutes"], 5)  # preserved
            self.assertEqual(result["target_volume"], 100)  # clamped

    def test_pixen_update_action_runs_installed_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            repo = RomRepository(Path(tmp) / "roms", Path(tmp) / "bios")
            with mock.patch.object(
                overmind_actions,
                "run_pixen_upgrade",
                return_value={"type": "pixen_update", "status": "started", "pid": 123},
            ) as run_mock:
                status, message, result = _execute_overmind_action(
                    settings,
                    repo,
                    {"action": "run_pixen_update"},
                )
            self.assertEqual(status, "completed")
            self.assertIn("PixeN", message)
            self.assertEqual(result["type"], "pixen_update")
            run_mock.assert_called_once_with(settings)


class IdleGameExitOvermindActionTests(unittest.TestCase):
    def setUp(self) -> None:
        drone_api._IDLE_GAME_EXIT_LAST_ARMED_ACTIVITY = None

    def test_action_saves_config_and_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            repo = RomRepository(Path(tmp) / "roms", Path(tmp) / "bios")
            status, message, result = _execute_overmind_action(
                settings,
                repo,
                {
                    "action": "set_idle_game_exit_automation",
                    "payload": {"enabled": True, "idle_minutes": 20},
                },
            )
            self.assertEqual(status, "completed")
            self.assertEqual(result["type"], "idle_game_exit_automation")
            self.assertEqual(result["enabled"], True)
            self.assertEqual(result["idle_minutes"], 20)
            self.assertIn("enabled", message)
            stored = _load_automation_config(settings)["idle_game_exit"]
            self.assertEqual(stored, {"enabled": True, "idle_minutes": 20})

    def test_action_partial_payload_merges_and_clamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            repo = RomRepository(Path(tmp) / "roms", Path(tmp) / "bios")
            _save_automation_config(
                settings,
                {"idle_game_exit": {"enabled": True, "idle_minutes": 15}},
            )
            status, _message, result = _execute_overmind_action(
                settings, repo, {"action": "set_idle_game_exit_automation", "payload": {"idle_minutes": 9999}}
            )
            self.assertEqual(status, "completed")
            self.assertEqual(result["enabled"], True)  # preserved
            self.assertEqual(result["idle_minutes"], 1440)  # clamped

    def test_action_does_not_disturb_idle_volume_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            repo = RomRepository(Path(tmp) / "roms", Path(tmp) / "bios")
            _save_automation_config(
                settings,
                {"idle_volume": {"enabled": True, "idle_minutes": 5, "target_volume": 25}},
            )
            _execute_overmind_action(
                settings,
                repo,
                {"action": "set_idle_game_exit_automation", "payload": {"enabled": True, "idle_minutes": 20}},
            )
            stored = _load_automation_config(settings)["idle_volume"]
            self.assertEqual(stored, {"enabled": True, "idle_minutes": 5, "target_volume": 25})


class IdleVolumeOvermindPushTests(unittest.TestCase):
    def test_collect_system_info_includes_idle_volume_automation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _save_automation_config(
                settings,
                {"idle_volume": {"enabled": True, "idle_minutes": 8, "target_volume": 10}},
            )
            payload = drone_api._collect_system_info_payload(settings)
            self.assertEqual(
                payload["idle_volume_automation"],
                {"enabled": True, "idle_minutes": 8, "target_volume": 10},
            )

    def test_collect_system_info_includes_idle_game_exit_automation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _save_automation_config(
                settings,
                {"idle_game_exit": {"enabled": True, "idle_minutes": 20}},
            )
            payload = drone_api._collect_system_info_payload(settings)
            self.assertEqual(
                payload["idle_game_exit_automation"],
                {"enabled": True, "idle_minutes": 20},
            )

    def test_collect_system_info_reports_pixen_installation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            script = settings.userdata_root / "roms" / "rgs" / "rgs_upgrade.sh"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            payload = drone_api._collect_system_info_payload(settings)
            self.assertTrue(payload["pixen_installed"])
            self.assertEqual(payload["pixen_script_path"], str(script.resolve()))

    def test_push_sends_full_heartbeat_with_current_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _save_automation_config(
                settings,
                {"idle_volume": {"enabled": False, "idle_minutes": 8, "target_volume": 0}},
            )
            with mock.patch.object(drone_api._local_network, "is_overmind_mode", return_value=True), \
                 mock.patch.object(
                     automation,
                     "_load_overmind_config_for_settings",
                     return_value={"overmind_url": "https://overmind.local/", "overmind_token": "tok"},
                 ), \
                 mock.patch.object(automation, "_overmind_post_json", return_value={}) as post_mock:
                ok = _push_automation_config_to_overmind(settings)
            self.assertTrue(ok)
            post_mock.assert_called_once()
            url = post_mock.call_args.args[0]
            body = post_mock.call_args.args[1]
            self.assertTrue(url.endswith("/heartbeat"))
            # A full system_info snapshot (not a partial one) is sent.
            self.assertIn("hostname", body["system_info"])
            self.assertEqual(
                body["system_info"]["idle_volume_automation"],
                {"enabled": False, "idle_minutes": 8, "target_volume": 0},
            )

    def test_push_no_op_when_overmind_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(drone_api._local_network, "is_overmind_mode", return_value=False), \
                 mock.patch.object(automation, "_overmind_post_json") as post_mock:
                self.assertFalse(_push_automation_config_to_overmind(settings))
                post_mock.assert_not_called()

    def test_push_swallows_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(drone_api._local_network, "is_overmind_mode", return_value=True), \
                 mock.patch.object(
                     automation,
                     "_load_overmind_config_for_settings",
                     return_value={"overmind_url": "https://overmind.local", "overmind_token": "tok"},
                 ), \
                 mock.patch.object(automation, "_overmind_post_json", side_effect=OSError("boom")):
                self.assertFalse(_push_automation_config_to_overmind(settings))


if __name__ == "__main__":
    unittest.main()
