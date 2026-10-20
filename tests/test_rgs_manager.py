import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.device import rgs_manager
from app.web.handlers_diagnostics import HandlersDiagnosticsMixin


class RgsManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = SimpleNamespace(userdata_root=Path(self.temp.name), use_fake_data=False)
        service = self.settings.userdata_root / "system/services/rgs_download"
        service.parent.mkdir(parents=True)
        service.touch()

    def test_missing_install_does_not_run_commands(self):
        (self.settings.userdata_root / "system/services/rgs_download").unlink()
        with mock.patch.object(rgs_manager.subprocess, "run") as run:
            self.assertFalse(rgs_manager.status(self.settings)["installed"])
            with self.assertRaises(OSError):
                rgs_manager.set_enabled(self.settings, False)
        run.assert_not_called()

    def test_reports_enabled_separately_from_running(self):
        with mock.patch.object(rgs_manager.shutil, "which", return_value="batocera-services"), \
             mock.patch.object(rgs_manager, "_run", return_value="other;*\nrgs_download;*\n"), \
             mock.patch.object(rgs_manager, "_running", return_value=False):
            state = rgs_manager.status(self.settings)
        self.assertTrue(state["available"])
        self.assertTrue(state["enabled"])
        self.assertFalse(state["running"])

    def test_toggle_controls_live_service_and_boot_preference(self):
        for enabled, actions in ((True, ["enable", "start"]), (False, ["stop", "disable"])):
            with self.subTest(enabled=enabled):
                before = dict(installed=True, available=True, enabled=not enabled, running=not enabled)
                after = dict(before, enabled=enabled, running=enabled)
                with mock.patch.object(rgs_manager, "status", side_effect=[before, after]), \
                     mock.patch.object(rgs_manager.shutil, "which", return_value="batocera-services"), \
                     mock.patch.object(rgs_manager.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")) as run:
                    self.assertEqual(rgs_manager.set_enabled(self.settings, enabled), after)
                self.assertEqual([call.args[0] for call in run.call_args_list],
                                 [["batocera-services", action, "rgs_download"] for action in actions])

    def test_rejects_non_boolean_without_commands(self):
        for value in (None, "false", 0, 1):
            with self.assertRaises(ValueError):
                rgs_manager.set_enabled(self.settings, value)

    def test_failed_start_is_reported(self):
        with mock.patch.object(rgs_manager, "status", return_value={"available": True}), \
             mock.patch.object(rgs_manager.shutil, "which", return_value="batocera-services"), \
             mock.patch.object(rgs_manager.subprocess, "run", side_effect=[
                 SimpleNamespace(returncode=0, stdout=""),
                 SimpleNamespace(returncode=1, stdout="", stderr="VPN route check failed"),
             ]):
            with self.assertRaisesRegex(OSError, "VPN route check failed"):
                rgs_manager.set_enabled(self.settings, True)

    def test_success_exit_without_state_change_is_not_success(self):
        with mock.patch.object(rgs_manager, "status", return_value={"available": True, "running": True, "enabled": False}), \
             mock.patch.object(rgs_manager, "_run"), \
             mock.patch.object(rgs_manager.time, "monotonic", side_effect=[0, 21]):
            with self.assertRaisesRegex(OSError, "did not reach"):
                rgs_manager.set_enabled(self.settings, False)

    def test_process_detection_matches_rgs_script_and_working_directory(self):
        script = self.settings.userdata_root / "system/rgs/download_service/download_service.py"
        script.parent.mkdir(parents=True)
        proc = self.settings.userdata_root / "proc"
        process = proc / "123"
        process.mkdir(parents=True)
        (process / "cwd").symlink_to(script.parent, target_is_directory=True)
        real_path = Path
        def path(value):
            return proc if value == "/proc" else real_path(value)
        for argument in (str(script), "download_service.py", "./download_service.py"):
            with self.subTest(argument=argument):
                (process / "cmdline").write_bytes(b"python3\0" + argument.encode() + b"\0")
                with mock.patch.object(rgs_manager, "Path", side_effect=path):
                    self.assertTrue(rgs_manager._running(self.settings))
        (process / "cwd").unlink()
        (process / "cwd").symlink_to(self.settings.userdata_root, target_is_directory=True)
        with mock.patch.object(rgs_manager, "Path", side_effect=path):
            self.assertFalse(rgs_manager._running(self.settings))

    def test_status_failure_disables_control(self):
        with mock.patch.object(rgs_manager.shutil, "which", return_value="batocera-services"), \
             mock.patch.object(rgs_manager.subprocess, "run", side_effect=OSError("permission denied")):
            state = rgs_manager.status(self.settings)
        self.assertFalse(state["available"])
        self.assertIsNone(state["running"])
        self.assertIn("permission denied", state["error"])

    def test_handler_maps_errors(self):
        handler = HandlersDiagnosticsMixin()
        handler.settings = self.settings
        handler._send_json = mock.Mock()
        handler._handle_admin_rgs_post({"enabled": "false"})
        self.assertEqual(handler._send_json.call_args.args[0], 400)
        with mock.patch.object(rgs_manager, "set_enabled", side_effect=OSError("failed")):
            handler._handle_admin_rgs_post({"enabled": False})
        self.assertEqual(handler._send_json.call_args.args[0], 503)
