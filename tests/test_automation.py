import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
import app.device.automation as automation
import app.device.device_control as device_control
from app.drone_api import (
    Settings,
    _load_automation_config,
    _normalize_idle_game_exit_config,
    _normalize_idle_volume_config,
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


class WifiRecoveryAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        automation._reset_wifi_recovery_check_state()

    def _enable(self, settings: Settings) -> None:
        _save_automation_config(settings, {"wifi_recovery": {"enabled": True}})

    def test_defaults_disabled_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertEqual(_load_automation_config(settings)["wifi_recovery"], {"enabled": False})
            self._enable(settings)
            self.assertEqual(_load_automation_config(settings)["wifi_recovery"], {"enabled": True})
            self.assertFalse(_load_automation_config(settings)["idle_volume"]["enabled"])

    def test_healthy_wifi_does_not_recover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings)
            with mock.patch.object(
                automation,
                "_wifi_health",
                return_value={"wifi_enabled": True, "wifi_connected": True, "wireless_interfaces": ["wlan0"]},
            ), mock.patch.object(automation, "_recover_wifi") as recover_mock:
                automation._run_wifi_recovery_automation_once(settings, now_monotonic=100)
            recover_mock.assert_not_called()

    def test_disabled_or_disconnected_wifi_recovers_once_per_minute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings)
            with mock.patch.object(
                automation,
                "_wifi_health",
                return_value={"wifi_enabled": False, "wifi_connected": False, "wireless_interfaces": ["wlan0"]},
            ), mock.patch.object(automation, "_recover_wifi") as recover_mock:
                automation._run_wifi_recovery_automation_once(settings, now_monotonic=100)
                automation._run_wifi_recovery_automation_once(settings, now_monotonic=159)
                automation._run_wifi_recovery_automation_once(settings, now_monotonic=160)
            self.assertEqual(recover_mock.call_count, 2)

    def test_recovery_runs_requested_command_sequence(self) -> None:
        with mock.patch.object(automation.shutil, "which", return_value="/usr/bin/batocera-wifi"), \
             mock.patch.object(automation.subprocess, "run") as run_mock, \
             mock.patch.object(automation.time, "sleep") as sleep_mock:
            automation._recover_wifi()
        self.assertEqual(
            run_mock.call_args_list,
            [
                mock.call(["/usr/bin/batocera-wifi", "disable"], check=True, timeout=30),
                mock.call(["/usr/bin/batocera-wifi", "enable"], check=True, timeout=30),
            ],
        )
        sleep_mock.assert_called_once_with(3)


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

    def test_already_at_target_does_not_reapply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings, idle_minutes=5, target=25)
            idle = time.time() - 600
            with mock.patch.object(automation, "_read_last_input_activity", return_value=idle), \
                 mock.patch.object(automation, "_get_audio_volume", return_value=25), \
                 mock.patch.object(automation, "_apply_audio_volume") as apply_mock:
                _run_idle_volume_automation_once(settings)
                apply_mock.assert_not_called()

    def test_current_below_target_raises_volume(self) -> None:
        # The automation sets the volume to the configured target regardless of
        # direction -- a target above the current volume must raise it, not just
        # lower it. (Previously the runner only ever lowered, so a target higher
        # than the current volume silently did nothing forever.)
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings, idle_minutes=5, target=80)
            idle = time.time() - 600
            with mock.patch.object(automation, "_read_last_input_activity", return_value=idle), \
                 mock.patch.object(automation, "_get_audio_volume", return_value=20), \
                 mock.patch.object(automation, "_apply_audio_volume", return_value=80) as apply_mock:
                _run_idle_volume_automation_once(settings)
                apply_mock.assert_called_once_with(settings, 80)


_RUNNING_GAME = {"pid": 123, "rom_path": "/userdata/roms/snes/game.sfc"}


class EmulatorKillControlTests(unittest.TestCase):
    def test_root_kill_waits_for_command_success(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch.object(device_control.os, "geteuid", return_value=0), \
             mock.patch.object(device_control, "_emulator_kill_command", return_value=["swissknife", "--emukill"]), \
             mock.patch.object(device_control.subprocess, "run", return_value=completed) as run:
            self.assertTrue(device_control._kill_running_emulator())
        run.assert_called_once()

    def test_root_kill_reports_command_failure(self) -> None:
        completed = mock.Mock(returncode=1)
        with mock.patch.object(device_control.os, "geteuid", return_value=0), \
             mock.patch.object(device_control, "_emulator_kill_command", return_value=["swissknife", "--emukill"]), \
             mock.patch.object(device_control.subprocess, "run", return_value=completed):
            with self.assertRaises(OSError):
                device_control._kill_running_emulator()


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

    def test_unavailable_kill_command_does_not_arm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self._enable(settings, idle_minutes=5)
            idle = time.time() - 600
            with mock.patch.object(automation, "_find_running_emulatorlauncher", return_value=_RUNNING_GAME), \
                 mock.patch.object(automation, "_read_last_input_activity", return_value=idle), \
                 mock.patch.object(automation, "_kill_running_emulator", return_value=False) as kill_mock:
                _run_idle_game_exit_automation_once(settings)
                _run_idle_game_exit_automation_once(settings)
                self.assertEqual(kill_mock.call_count, 2)


class SystemInfoPayloadAutomationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
