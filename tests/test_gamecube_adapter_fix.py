import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.device import admin_fixes
from app.device.fix_assets import gamecube_adapter_fix as fix
from app.drone_api import Settings


def build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.db"),
        "DRONE_DEVICE_ID": "drone-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class FakeHost:
    """sysfs/proc tree: an adapter plus wii-u-gc-adapter processes with threads."""

    ADAPTER = "5-1.4.4"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.devices = root / "sys" / "bus" / "usb" / "devices"
        self.proc = root / "proc"
        self.run = root / "run"
        self.devices.mkdir(parents=True)
        self.proc.mkdir()
        self.daemon = root / "bin" / "wii-u-gc-adapter"
        self.daemon.parent.mkdir()
        self.daemon.write_text("#!/bin/sh\n", encoding="utf-8")
        self.add_usb_device("3-1", "054c", "09cc", devnum=4)

    def add_usb_device(self, name, vendor="057e", product="0337", devnum=19, bound=True):
        device = self.devices / name
        if device.exists():
            for child in list(device.iterdir()):
                child.rmdir() if child.is_dir() else child.unlink()
            device.rmdir()
        device.mkdir()
        for key, value in (("idVendor", vendor), ("idProduct", product), ("busnum", "5"), ("devnum", str(devnum))):
            (device / key).write_text(value + "\n", encoding="utf-8")
        if bound:
            (device / "driver").mkdir()
        (self.devices / f"{name}:1.0").mkdir(exist_ok=True)

    def unbind(self, name=ADAPTER):
        (self.devices / name / "driver").rmdir()

    def add_process(self, pid, argv, threads=3):
        folder = self.proc / str(pid)
        folder.mkdir()
        (folder / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
        for index in range(threads):
            (folder / "task" / str(pid + index)).mkdir(parents=True)

    def remove_process(self, pid):
        folder = self.proc / str(pid)
        for task in (folder / "task").iterdir():
            task.rmdir()
        (folder / "task").rmdir()
        (folder / "cmdline").unlink()
        folder.rmdir()

    def patches(self):
        return [
            mock.patch.object(fix, "USB_DEVICES_ROOT", self.devices),
            mock.patch.object(fix, "PROC_ROOT", self.proc),
            mock.patch.object(fix, "RUNTIME_ROOT", self.run),
            mock.patch.object(fix, "GUARD_STATE_PATH", self.run / "guard.json"),
            mock.patch.object(fix, "GUARD_LOCK_PATH", self.run / "guard.lock"),
            mock.patch.object(fix, "LOG_PATH", self.root / "fix.log"),
            mock.patch.object(fix, "CONFIG_PATH", self.root / "config.json"),
        ]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class GameCubeAdapterWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.host = FakeHost(Path(self._tmp.name))
        for patcher in self.host.patches():
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = fix.load_config()
        self.config["daemon_path"] = str(self.host.daemon)
        self.clock = Clock()
        self.repairs = []
        self.toasts = []
        self.toast_ok = True
        self.watcher = fix.Watcher(self.config, clock=self.clock, repairer=self._repair, notifier=self._notify)

    def _repair(self, config, daemons):
        self.repairs.append(list(daemons))
        return True

    def _notify(self, message):
        self.toasts.append(message)
        return self.toast_ok

    def settle(self):
        """First tick records the state; the next one after settle_seconds judges it."""
        first = self.watcher.tick()
        self.clock.now += self.config["settle_seconds"] + 0.1
        return first, self.watcher.tick()

    def test_healthy_daemon_with_reader_thread_is_left_alone(self) -> None:
        self.host.add_usb_device(FakeHost.ADAPTER)
        self.host.add_process(27317, ["/usr/bin/wii-u-gc-adapter"], threads=3)
        self.assertEqual(self.settle(), ("settling", "healthy"))
        self.assertEqual(self.repairs, [])

    def test_stuck_daemon_without_reader_thread_is_replaced_after_settling(self) -> None:
        # The state observed on a real Batocera v43.1 box: two threads, no reader.
        self.host.add_usb_device(FakeHost.ADAPTER)
        self.host.add_process(27317, ["/usr/bin/wii-u-gc-adapter"], threads=2)
        self.assertEqual(self.settle(), ("settling", "repaired"))
        self.assertEqual(self.repairs, [[27317]])

    def test_missing_and_duplicate_daemons_are_repaired(self) -> None:
        self.host.add_usb_device(FakeHost.ADAPTER)
        self.assertEqual(self.settle()[1], "repaired")
        self.assertEqual(self.repairs, [[]])
        self.host.add_process(10, ["/usr/bin/wii-u-gc-adapter"])
        self.host.add_process(20, ["/usr/bin/wii-u-gc-adapter"])
        self.assertEqual(self.settle()[1], "repaired")
        self.assertEqual(self.repairs[-1], [10, 20])

    def test_never_acts_during_the_udev_burst(self) -> None:
        self.host.add_usb_device(FakeHost.ADAPTER)
        self.host.add_process(10, ["/usr/bin/wii-u-gc-adapter"], threads=2)
        self.host.add_process(11, ["/bin/bash", "/usr/bin/wii-u-gc-adapter-daemon"], threads=1)
        self.assertEqual(self.settle(), ("settling", "settling"))
        # Each change in the daemon set restarts the settle timer.
        self.host.remove_process(11)
        self.host.remove_process(10)
        self.host.add_process(12, ["/usr/bin/wii-u-gc-adapter"], threads=3)
        self.clock.now += 1
        self.assertEqual(self.watcher.tick(), "settling")
        self.assertEqual(self.repairs, [])

    def test_no_adapter_or_unbound_adapter_or_guard_active_means_no_action(self) -> None:
        self.assertEqual(self.settle(), ("idle", "idle"))
        self.host.add_usb_device(FakeHost.ADAPTER, bound=False)
        self.assertEqual(self.settle(), ("idle", "idle"))
        self.host.add_usb_device(FakeHost.ADAPTER)
        self.host.run.mkdir(exist_ok=True)
        (self.host.run / "guard.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.settle(), ("idle", "idle"))
        self.assertEqual(self.repairs, [])
        self.assertEqual(self.toasts, [])

    def test_gives_up_after_max_repairs_and_shows_one_emulationstation_message(self) -> None:
        self.host.add_usb_device(FakeHost.ADAPTER)
        self.host.add_process(10, ["/usr/bin/wii-u-gc-adapter"], threads=2)
        for _ in range(self.config["max_repairs"]):
            self.assertEqual(self.settle()[1], "repaired")
        self.assertEqual(self.settle()[1], "gave_up")
        self.assertEqual(self.toasts, [fix.FAILURE_TOAST])
        self.assertLessEqual(len(fix.FAILURE_TOAST), fix.TOAST_MAX_CHARS)
        self.assertEqual(self.settle()[1], "gave_up")
        self.assertEqual(len(self.repairs), self.config["max_repairs"])
        self.assertEqual(len(self.toasts), 1)

    def test_message_is_retried_until_emulationstation_accepts_it(self) -> None:
        self.config["max_repairs"] = 1
        self.toast_ok = False
        self.host.add_usb_device(FakeHost.ADAPTER)
        self.host.add_process(10, ["/usr/bin/wii-u-gc-adapter"], threads=2)
        self.settle()
        self.assertEqual(self.settle()[1], "gave_up")
        self.toast_ok = True
        self.watcher.tick()
        # One refused attempt when giving up, then the retry that lands.
        self.assertEqual(self.toasts, [fix.FAILURE_TOAST] * 2)
        self.watcher.tick()
        self.assertEqual(len(self.toasts), 2)

    def test_replugging_gives_the_new_plug_in_fresh_attempts(self) -> None:
        self.config["max_repairs"] = 1
        self.host.add_usb_device(FakeHost.ADAPTER)
        self.host.add_process(10, ["/usr/bin/wii-u-gc-adapter"], threads=2)
        self.settle()
        self.assertEqual(self.settle()[1], "gave_up")
        self.host.add_usb_device(FakeHost.ADAPTER, devnum=20)  # new devnum = new plug-in
        self.assertEqual(self.settle()[1], "repaired")

    def test_missing_driver_binary_shows_a_message_without_repairing(self) -> None:
        self.host.daemon.unlink()
        self.host.add_usb_device(FakeHost.ADAPTER)
        self.assertEqual(self.settle()[1], "gave_up")
        self.assertEqual(self.repairs, [])
        self.assertEqual(self.toasts, [fix.MISSING_DAEMON_TOAST])
        self.assertLessEqual(len(fix.MISSING_DAEMON_TOAST), fix.TOAST_MAX_CHARS)

    def test_repair_stops_every_instance_and_starts_exactly_one(self) -> None:
        self.host.add_process(10, ["/usr/bin/wii-u-gc-adapter"])
        self.host.add_process(20, ["/usr/bin/wii-u-gc-adapter"])
        with mock.patch.object(fix, "stop_process") as stop, mock.patch.object(fix, "start_daemon") as start, \
                mock.patch.object(fix.time, "sleep"):
            self.assertTrue(fix.repair(self.config, [10, 20]))
        self.assertEqual([call.args[0] for call in stop.call_args_list], [10, 20])
        start.assert_called_once_with(self.config)

    def test_repair_defers_to_an_active_switch2_guard(self) -> None:
        self.host.run.mkdir()
        (self.host.run / "guard.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(fix, "stop_process") as stop, mock.patch.object(fix, "start_daemon") as start:
            self.assertFalse(fix.repair(self.config, [10]))
        stop.assert_not_called()
        start.assert_not_called()

    def test_config_values_are_clamped(self) -> None:
        (self.host.root / "config.json").write_text(json.dumps(
            {"interval_seconds": 0, "settle_seconds": 9999, "max_repairs": 50, "daemon_path": "relative"}), encoding="utf-8")
        config = fix.load_config()
        self.assertEqual(config["interval_seconds"], 0.5)
        self.assertEqual(config["settle_seconds"], 120.0)
        self.assertEqual(config["max_repairs"], 10)
        self.assertEqual(config["daemon_path"], "/usr/bin/wii-u-gc-adapter")


class GameCubeAdapterFixLifecycleTests(unittest.TestCase):
    def test_enable_and_disable_are_reversible_and_never_touch_host_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = build_settings(root)
            fix_id = admin_fixes.GAMECUBE_ADAPTER_FIX_ID
            service = root / "system" / "services" / admin_fixes.GAMECUBE_ADAPTER_SERVICE_NAME
            conf = root / "system" / "batocera.conf"
            self.assertEqual(admin_fixes.get_fix(settings, fix_id)["status"], "disabled")
            with mock.patch.object(admin_fixes.subprocess, "run") as run:
                enabled = admin_fixes.set_fix_enabled(settings, fix_id, True)
            run.assert_not_called()
            self.assertTrue(service.stat().st_mode & 0o100)
            self.assertEqual(service.read_bytes(), admin_fixes.GAMECUBE_ADAPTER_FIX_ASSET.read_bytes())
            config = json.loads((root / "system" / "gamecube-adapter-fix" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["usb_ids"], ["057e:0337"])
            self.assertEqual(enabled["log_path"], str(root / "system" / "logs" / "gamecube-adapter-fix.log"))
            # Not in system.services means Batocera will not start it at boot.
            self.assertEqual(enabled["status"], "modified")
            conf.write_text("system.services=rgs_config DRONE_GC_ADAPTER DRONE_SERVER\n", encoding="utf-8")
            self.assertEqual(admin_fixes.get_fix(settings, fix_id)["status"], "enabled")
            service.write_text("# edited\n", encoding="utf-8")
            self.assertEqual(admin_fixes.get_fix(settings, fix_id)["status"], "modified")
            with mock.patch.object(admin_fixes.subprocess, "run") as run:
                disabled = admin_fixes.set_fix_enabled(settings, fix_id, False)
            run.assert_not_called()
            self.assertFalse(disabled["enabled"])
            self.assertFalse(service.exists())

    def test_real_batocera_enables_and_disables_the_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            ok = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(admin_fixes, "_on_real_batocera", return_value=True), \
                    mock.patch.object(admin_fixes.subprocess, "run", return_value=ok) as run:
                admin_fixes.set_fix_enabled(settings, admin_fixes.GAMECUBE_ADAPTER_FIX_ID, True)
                calls = [call.args[0] for call in run.call_args_list]
                self.assertEqual(calls, [["batocera-services", "enable", "DRONE_GC_ADAPTER"],
                                         ["batocera-services", "restart", "DRONE_GC_ADAPTER"]])
                run.reset_mock()
                admin_fixes.set_fix_enabled(settings, admin_fixes.GAMECUBE_ADAPTER_FIX_ID, False)
                calls = [call.args[0] for call in run.call_args_list]
                self.assertEqual(calls[0][1:], ["stop"])
                self.assertEqual(calls[1], ["batocera-services", "disable", "DRONE_GC_ADAPTER"])

    def test_service_failure_is_reported_as_os_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            failed = mock.Mock(returncode=1, stdout="", stderr="boom")
            with mock.patch.object(admin_fixes, "_on_real_batocera", return_value=True), \
                    mock.patch.object(admin_fixes.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(OSError, "boom"):
                    admin_fixes.set_fix_enabled(settings, admin_fixes.GAMECUBE_ADAPTER_FIX_ID, True)
            with mock.patch.object(admin_fixes, "_on_real_batocera", return_value=True), \
                    mock.patch.object(admin_fixes.subprocess, "run",
                                      side_effect=admin_fixes.subprocess.TimeoutExpired("x", 30)):
                with self.assertRaises(OSError):
                    admin_fixes.set_fix_enabled(settings, admin_fixes.GAMECUBE_ADAPTER_FIX_ID, True)

    def test_service_name_is_valid_for_batocera_services(self) -> None:
        import re
        self.assertRegex(admin_fixes.GAMECUBE_ADAPTER_SERVICE_NAME, re.compile(r"^[_A-Za-z][_A-Za-z0-9]*$"))


if __name__ == "__main__":
    unittest.main()
