from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from app.device.fix_assets import sinden_recoil as recoil


class SindenRecoilHookTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ("drivers", "proc", "runtime", "logs"):
            (self.root / name).mkdir()
        for attr, path in {
            "RUN": self.root,
            "DRIVER_ROOT": self.root / "drivers",
            "PROC": self.root / "proc",
            "RUNTIME": self.root / "runtime",
            "DRIVER_LOG_ROOT": self.root / "logs",
            "CONFIG": self.root / "config.json",
            "SYSTEM_ROOT": self.root,
        }.items():
            stack.enter_context(mock.patch.object(recoil, attr, path))
        self.rom = self.root / "roms/mame/custom/area51.zip"
        self.owner = [100, "300"]
        self.add_process(100, "300", ["/usr/bin/python", "/usr/bin/emulatorlauncher", "-rom", str(self.rom)])
        recoil.CONFIG.write_text(json.dumps({
            "enabled": True, "roms_root": str(self.root / "roms"),
            "profiles": {"mame/custom/area51.zip": "single"},
        }))

    def add_process(self, pid, start, args):
        folder = recoil.PROC / str(pid)
        folder.mkdir(exist_ok=True)
        (folder / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in args) + b"\0")
        (folder / "stat").write_text(f"{pid} (process) S " + "0 " * 18 + start)
        return folder

    def add_gun(self, device_hash, mapper_pid, driver_pid):
        gun = recoil.Gun(device_hash)
        gun.directory.mkdir()
        gun.config.write_text("""<configuration><appSettings>
            <add key="SerialPortWrite" value="/dev/ttyACM0"/>
            <add key="CameraExposure" value="-7"/>
            <add key="EnableRecoil" value="0"/>
            <add key="TriggerRecoilNormalOrRepeat" value="1"/>
            </appSettings></configuration>""")
        gun.mapper.write_text(str(mapper_pid))
        self.add_process(mapper_pid, "201", ["/usr/bin/evsieve", "--output", "name=Sinden lightgun"])
        gun.lockfile.write_text(str(driver_pid))
        self.add_process(driver_pid, "202", gun.command)
        return gun

    def start(self):
        # v44 can pass a temporary mount path as the hook argument.
        recoil.start("mame", "/tmp/rom-mount", self.owner[0], self.owner)

    def test_single_shot_session_restores_both_guns_at_game_stop(self):
        one = self.add_gun("a" * 32, 101, 102)
        two = self.add_gun("b" * 32, 103, 104)
        preset = recoil.PRESETS["single"][1]
        original = {"EnableRecoil": "0", "TriggerRecoilNormalOrRepeat": "1"}
        events = []

        def restart(gun, values, mapper):
            self.assertIsNotNone(recoil.load_session())
            events.append((gun.device_hash, values, mapper))
            return True

        with mock.patch.object(recoil.Gun, "restart", autospec=True, side_effect=restart), \
             mock.patch.object(recoil.subprocess, "Popen") as spawn, \
             mock.patch.object(recoil.os, "getppid", return_value=self.owner[0]):
            self.start()
            self.assertTrue(spawn.call_args.kwargs["start_new_session"])
            self.assertEqual(recoil.load_session()["game"], "mame/custom/area51.zip")
            self.assertEqual(recoil.main(["gameStop", "mame", "mame", "", str(self.rom)]), 0)
        self.assertEqual(events, [
            (one.device_hash, preset, [101, "201"]), (two.device_hash, preset, [103, "201"]),
            (one.device_hash, original, [101, "201"]), (two.device_hash, original, [103, "201"]),
        ])
        self.assertIsNone(recoil.load_session())

    def test_watchdog_handles_reused_launcher_pid_and_retries_failed_restore(self):
        gun = self.add_gun("a" * 32, 101, 102)
        with mock.patch.object(recoil.subprocess, "Popen"), mock.patch.object(recoil.Gun, "restart", return_value=True):
            self.start()
        self.add_process(100, "301", ["different-process"])
        with mock.patch.object(recoil.time, "sleep"), \
             mock.patch.object(recoil.Gun, "restart", side_effect=[OSError("busy"), True]) as restart, \
             self.assertLogs(recoil.LOG, level="WARNING"):
            recoil.watch(self.owner)
        self.assertEqual(restart.call_count, 2)
        self.assertEqual(restart.call_args.args[0], gun.config_values(recoil.PRESETS["single"][1]))
        self.assertIsNone(recoil.load_session())

    def test_failure_on_second_gun_rolls_back_both(self):
        self.add_gun("a" * 32, 101, 102)
        self.add_gun("b" * 32, 103, 104)
        with mock.patch.object(recoil.subprocess, "Popen"), \
             mock.patch.object(recoil.Gun, "restart", side_effect=[True, OSError("busy"), True, True]) as restart, \
             self.assertLogs(recoil.LOG, level="ERROR"):
            self.start()
        self.assertEqual([call.args[0]["EnableRecoil"] for call in restart.call_args_list], ["1", "1", "0", "0"])
        self.assertIsNone(recoil.load_session())

    def test_unconfigured_launch_does_not_touch_drivers(self):
        self.add_gun("a" * 32, 101, 102)
        with mock.patch.object(recoil.Gun, "restart") as restart:
            recoil.start("fbneo", str(self.rom), self.owner[0], self.owner)
        restart.assert_not_called()
        self.assertIsNone(recoil.load_session())

    def test_restart_keeps_mapper_and_preserves_other_config_values(self):
        gun = self.add_gun("a" * 32, 101, 102)
        child = mock.Mock(pid=103)
        child.poll.return_value = None

        def spawn(*args, **kwargs):
            self.add_process(child.pid, "203", gun.command)
            gun.lockfile.write_text(str(child.pid))
            return child

        with mock.patch.object(gun, "stop_driver") as stop, \
             mock.patch.object(recoil.subprocess, "Popen", side_effect=spawn) as start, \
             mock.patch.object(recoil, "driver_has_devices", return_value=True) as ready:
            self.assertTrue(gun.restart(recoil.PRESETS["single"][1], [101, "201"]))
        stop.assert_called_once_with([102, "202"])
        ready.assert_called_once_with(103, "/dev/ttyACM0")
        self.assertEqual(start.call_args.args[0], gun.command)
        child.wait.assert_not_called()
        self.assertEqual(gun.mapper_identity(), [101, "201"])
        self.assertEqual(gun.config_values(["TriggerRecoilNormalOrRepeat", "CameraExposure"]), {
            "TriggerRecoilNormalOrRepeat": "0", "CameraExposure": "-7",
        })
