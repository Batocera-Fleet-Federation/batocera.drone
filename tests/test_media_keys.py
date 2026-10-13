"""Media Keys integration: bindings, dispatcher, hotkeygen override, shared stores, HTTP routes."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from unittest import mock

from app.integrations.media_keys import dispatch as dispatch_module
from app.integrations.media_keys.config import ConfigStore, MANAGED_KEYS, validate_binding
from app.integrations.media_keys.dispatch import HoldGate, handle_press
from app.integrations.media_keys.hotkeygen import HotkeygenOverrides, MARKER, detect_keyboards, render_override
from app.integrations.media_keys.manager import MediaKeysIntegration
from app.integrations.media_keys.paths import MediaKeysPaths
from app.integrations.shared.batocera_control import BUILTIN_COMMANDS, BatoceraControl
from app.integrations.shared.process import ProcessResult
from app.integrations.streamdeck.paths import OwnershipError


class FakeRunner:
    def __init__(self, exit_code=0):
        self.calls = []
        self.exit_code = exit_code

    def run(self, executable, arguments=(), **kwargs):
        self.calls.append((str(executable), [str(a) for a in arguments], kwargs))
        return ProcessResult(self.exit_code, duration_seconds=0.01)


class FakeControl:
    def __init__(self):
        self.calls = []

    def run_builtin(self, action_id):
        self.calls.append(action_id)
        return {"status": "ok"}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / "drone-app" / "integrations" / "media-keys"
        self.paths = MediaKeysPaths(self.root)
        self.user_dir = self.base / "hotkeygen-user"
        self.overrides = HotkeygenOverrides(self.paths, user_dir=self.user_dir, stock_dir=self.base / "etc",
                                            proc_root=self.base / "proc")
        patcher = mock.patch.object(dispatch_module, "_drone_log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def manager(self, **kwargs):
        kwargs.setdefault("control", FakeControl())
        kwargs.setdefault("keyboard_detector", lambda: [])
        return MediaKeysIntegration(mock.Mock(), mock.Mock(), paths=self.paths, overrides=self.overrides, **kwargs)


class ConfigTests(Base):
    def test_defaults_match_the_issue(self):
        config = ConfigStore(self.paths).load()
        self.assertFalse(config["enabled"])
        actions = {key: binding.get("action_id") for key, binding in config["bindings"].items()}
        self.assertEqual(actions, {"volume-up": "volume-up", "volume-down": "volume-down", "mute": "volume-mute",
                                   "exit": "exit-game", "reboot": "reboot"})
        self.assertGreaterEqual(config["bindings"]["reboot"]["hold_seconds"], 2)

    def test_validation_rejects_raw_commands_and_bad_ids(self):
        for raw in ({"action_type": "command", "command": "rm -rf /"}, {"action_type": "builtin", "action_id": "rm -rf /"},
                    {"action_type": "script", "script_id": "../../etc/passwd"}, {"action_type": "builtin"}, "x"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_binding("exit", raw)
        with self.assertRaises(ValueError):
            validate_binding("KEY_POWER", {"action_type": "none"})
        with self.assertRaises(ValueError):
            validate_binding("exit", {"action_type": "builtin", "action_id": "exit-game", "hold_seconds": 99})

    def test_reboot_cannot_be_saved_without_a_hold(self):
        binding = validate_binding("reboot", {"action_type": "builtin", "action_id": "reboot", "hold_seconds": 0})
        self.assertGreaterEqual(binding["hold_seconds"], 2)

    def test_corrupt_config_falls_back_to_defaults(self):
        self.paths.ensure_layout()
        self.paths.config_file.write_text("{not json")
        self.assertFalse(ConfigStore(self.paths).load()["enabled"])


class DispatcherTests(Base):
    def setUp(self):
        super().setUp()
        self.paths.ensure_layout()
        self.control = FakeControl()
        self.now = [1000.0]

    def press(self, binding_id, **kwargs):
        return handle_press(binding_id, self.paths, control=self.control, clock=lambda: self.now[0], **kwargs)

    def test_disabled_integration_does_nothing(self):
        self.assertEqual(self.press("volume-up")["status"], "disabled")
        self.assertEqual(self.control.calls, [])

    def test_builtin_dispatch_runs_exactly_once(self):
        ConfigStore(self.paths).set_enabled(True)
        self.assertEqual(self.press("volume-up")["status"], "ok")
        self.assertEqual(self.press("mute")["status"], "ok")
        self.assertEqual(self.control.calls, ["volume-up", "volume-mute"])
        recorded = json.loads(self.paths.press_file.read_text())
        self.assertEqual(recorded["volume-up"]["status"], "ok")

    def test_unknown_binding_is_rejected(self):
        ConfigStore(self.paths).set_enabled(True)
        self.assertEqual(self.press("../etc")["status"], "unknown-binding")
        self.assertEqual(self.control.calls, [])

    def test_binding_edit_applies_on_next_press(self):
        store = ConfigStore(self.paths)
        store.set_enabled(True)
        store.set_binding("exit", {"action_type": "builtin", "action_id": "restart-frontend"})
        self.press("exit")
        self.assertEqual(self.control.calls, ["restart-frontend"])

    def test_reboot_needs_a_hold(self):
        ConfigStore(self.paths).set_enabled(True)
        self.assertEqual(self.press("reboot")["status"], "holding")  # a single press
        self.now[0] += 5  # released and pressed again much later: a new press, not a hold
        self.assertEqual(self.press("reboot")["status"], "holding")
        self.assertEqual(self.control.calls, [])

    def test_reboot_fires_once_after_a_sustained_hold(self):
        ConfigStore(self.paths).set_enabled(True)
        results = []
        # Kernel auto-repeat: ~0.6 s initial delay, then ~30 Hz until release.
        start = self.now[0]
        offsets = [0.0, 0.6] + [0.6 + 0.033 * step for step in range(1, 90)]
        for offset in offsets:
            self.now[0] = start + offset
            results.append(self.press("reboot")["status"])
        self.assertEqual(self.control.calls, ["reboot"])
        self.assertEqual(results.count("ok"), 1)

    def test_fast_separate_taps_do_not_confirm_reboot(self):
        ConfigStore(self.paths).set_enabled(True)
        results = []
        for _ in range(12):
            results.append(self.press("reboot")["status"])
            self.now[0] += 0.45
        self.assertEqual(self.control.calls, [])
        self.assertTrue(all(status == "holding" for status in results), results)

    def test_script_dispatch_uses_the_script_store(self):
        manager = self.manager()
        script = manager.scripts.create({"name": "Say", "code": "#!/bin/bash\necho hi\n"})
        manager.config.set_enabled(True)
        manager.set_binding("exit", {"action_type": "script", "script_id": script["id"]})
        runner = FakeRunner()
        store = dispatch_module.media_keys_script_store(self.paths, runner)
        result = self.press("exit", scripts=store)
        self.assertEqual(result["status"], "ok")
        executable, arguments, kwargs = runner.calls[0]
        self.assertEqual(Path(executable).parent, self.paths.scripts_dir)
        self.assertEqual(arguments, [])
        self.assertNotIn("shell", kwargs)
        env = kwargs["env"]
        self.assertEqual(set(env) - {"DRONE_MEDIA_KEYS_TRIGGER"}, {"PATH", "HOME", "LANG", "DRONE_MEDIA_KEYS_ROOT"})

    def test_failing_action_is_recorded_not_raised(self):
        ConfigStore(self.paths).set_enabled(True)
        self.control.run_builtin = mock.Mock(side_effect=RuntimeError("boom"))
        result = self.press("volume-up")
        self.assertEqual(result["status"], "error")
        self.assertIn("boom", json.loads(self.paths.press_file.read_text())["volume-up"]["error"])

    def test_hold_gate_gap_resets(self):
        gate = HoldGate(self.paths, clock=lambda: self.now[0])
        self.assertEqual(gate.check("reboot", 2), "holding")
        self.now[0] += 0.6
        self.assertEqual(gate.check("reboot", 2), "holding")
        self.now[0] += 3  # gap > 1 s: a fresh press
        self.assertEqual(gate.check("reboot", 2), "holding")

    def test_hold_gate_test_origin_accepts_ui_pulse_spacing(self):
        gate = HoldGate(self.paths, clock=lambda: self.now[0])
        outcomes = []
        for _ in range(10):
            outcomes.append(gate.check("reboot", 3, origin="test"))
            self.now[0] += 0.35
        self.assertEqual(outcomes.count("fire"), 1, outcomes)

    def test_hold_gate_key_and_test_origins_do_not_share_progress(self):
        gate = HoldGate(self.paths, clock=lambda: self.now[0])
        self.assertEqual(gate.check("reboot", 3, origin="key"), "holding")
        self.now[0] += 0.1
        self.assertEqual(gate.check("reboot", 3, origin="test"), "holding")
        self.now[0] += 0.1
        self.assertEqual(gate.check("reboot", 3, origin="key"), "holding")


class RootDispatcherStateOwnershipTests(Base):
    """hotkeygen runs the dispatcher as root; state/ must stay usable by the Drone user."""

    def setUp(self):
        super().setUp()
        self.paths.ensure_layout()

    def test_state_files_are_handed_to_the_integration_owner_when_root(self):
        owner = (4242, 4343)
        chowned = []
        with mock.patch.object(dispatch_module, "script_owner", return_value=owner), \
                mock.patch("os.fchown", side_effect=lambda fd, uid, gid: chowned.append((uid, gid))), \
                mock.patch("os.lchown", side_effect=lambda path, uid, gid: chowned.append((str(path), uid, gid))):
            HoldGate(self.paths, clock=lambda: 1000.0).check("reboot", 2)
            dispatch_module.record_press(self.paths, "reboot", {"status": "ok"})
        # hold.lock, hold.json and last-press.json each get the owner; the state dir too if foreign.
        self.assertGreaterEqual(chowned.count(owner), 3, chowned)

    def test_no_chown_when_not_root(self):
        with mock.patch.object(dispatch_module, "script_owner", return_value=None), \
                mock.patch("os.fchown") as fchown, mock.patch("os.lchown") as lchown:
            HoldGate(self.paths, clock=lambda: 1000.0).check("reboot", 2)
            dispatch_module.record_press(self.paths, "reboot", {"status": "ok"})
        fchown.assert_not_called()
        lchown.assert_not_called()

    def test_unopenable_stale_lock_is_recreated(self):
        lock = self.paths.state_dir / "hold.lock"
        lock.write_text("")
        real_open = os.open
        calls = []

        def flaky_open(path, *args, **kwargs):
            if str(path) == str(lock) and not calls:
                calls.append(path)
                raise PermissionError(13, "denied")
            return real_open(path, *args, **kwargs)

        with mock.patch("os.open", side_effect=flaky_open):
            self.assertEqual(HoldGate(self.paths, clock=lambda: 1000.0).check("reboot", 2), "holding")
        self.assertTrue(lock.exists())


class BuiltinCatalogTests(unittest.TestCase):
    def test_catalog_runs_reviewed_commands_only(self):
        runner = FakeRunner()
        control = BatoceraControl(runner, which=lambda name: f"/usr/bin/{name}")
        for action_id in BUILTIN_COMMANDS:
            if action_id == "restart-frontend":
                continue
            self.assertEqual(control.run_builtin(action_id)["status"], "ok", action_id)
        commands = [(Path(call[0]).name, call[1]) for call in runner.calls]
        self.assertIn(("batocera-es-swissknife", ["--emukill"]), commands)
        self.assertIn(("batocera-audio", ["setSystemVolume", "+5"]), commands)
        self.assertEqual(control.run_builtin("; rm -rf /")["status"], "error")
        self.assertEqual(len(runner.calls), len(BUILTIN_COMMANDS) - 1)


class HotkeygenTests(Base):
    def test_override_writes_only_managed_keys_and_is_idempotent(self):
        self.paths.ensure_layout()
        first = self.overrides.apply()
        self.assertTrue(first["changed"])
        text = self.overrides.override_path.read_text()
        self.assertTrue(text.startswith(MARKER))
        keys = [line.split(" = ")[0] for line in text.splitlines()[1:]]
        self.assertEqual(sorted(keys), sorted(key for key, _ in MANAGED_KEYS.values()))
        for line in text.splitlines()[1:]:
            self.assertIn(str(self.paths.launcher), line)
        self.assertFalse(self.overrides.apply()["changed"])
        self.assertTrue(self.overrides.applied())
        self.assertEqual(os.listdir(self.user_dir), ["drone-media-keys.conf"])
        self.assertTrue(os.access(self.paths.launcher, os.X_OK))

    def test_restore_removes_only_the_drone_file(self):
        self.paths.ensure_layout()
        self.overrides.apply()
        (self.user_dir / "user-own.conf").write_text("keep")
        self.assertTrue(self.overrides.restore()["removed"])
        self.assertEqual(os.listdir(self.user_dir), ["user-own.conf"])
        self.assertFalse(self.overrides.restore()["removed"])

    def test_foreign_file_with_the_same_name_is_never_overwritten_or_removed(self):
        self.paths.ensure_layout()
        self.user_dir.mkdir()
        self.overrides.override_path.write_text("mine\n")
        with self.assertRaises(ValueError):
            self.overrides.apply()
        self.assertFalse(self.overrides.restore()["removed"])
        self.assertEqual(self.overrides.override_path.read_text(), "mine\n")

    def test_unsafe_launcher_path_is_refused(self):
        with self.assertRaises(ValueError):
            render_override(Path("/tmp/a b;rm -rf"))

    def test_keyboard_detection_parses_proc_devices(self):
        devices = self.base / "devices"
        devices.write_text('I: Bus=0003\nN: Name="Acme USB Keyboard Consumer Control"\nH: Handlers=kbd event11\n\n'
                           'I: Bus=0019\nN: Name="Power Button"\nH: Handlers=event0\n')
        self.assertEqual(detect_keyboards(devices), [{"name": "Acme USB Keyboard Consumer Control", "event": "event11"}])


class ManagerLifecycleTests(Base):
    def test_enable_disable_remove(self):
        manager = self.manager()
        self.assertEqual(manager.card_status()["health"], "disabled")
        manager.enable(requested_by="admin")
        self.assertTrue(manager.config.load()["enabled"])
        self.assertTrue(self.overrides.applied())
        manager.set_binding("mute", {"action_type": "none"})
        manager.disable()
        self.assertFalse(self.overrides.override_path.exists())
        self.assertFalse(manager.config.load()["enabled"])
        self.assertEqual(manager.config.load()["bindings"]["mute"]["action_type"], "none")  # kept
        manager.enable()
        sibling = self.root.parent / "streamdeck"
        sibling.mkdir(parents=True)
        manager.remove(include_configuration=True)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.overrides.override_path.exists())
        self.assertTrue(sibling.exists())  # only media-keys/ is removed

    def test_remove_refuses_an_unexpected_root(self):
        other = MediaKeysPaths(self.base / "elsewhere")
        other.root.mkdir()
        with self.assertRaises(OwnershipError):
            other.remove_root(self.root)

    def test_binding_to_missing_script_is_rejected(self):
        manager = self.manager()
        with self.assertRaises(KeyError):
            manager.set_binding("exit", {"action_type": "script", "script_id": "a" * 32})

    def test_script_in_use_cannot_be_deleted(self):
        manager = self.manager()
        script = manager.scripts.create({"name": "Say", "code": "#!/bin/bash\necho hi\n"})
        manager.set_binding("exit", {"action_type": "script", "script_id": script["id"]})
        from app.integrations.shared.scripts import ScriptInUseError
        with self.assertRaises(ScriptInUseError):
            manager.scripts.delete(script["id"], manager.script_references(script["id"]), noun="key binding")

    def test_script_store_rejects_raw_command_bodies_and_bad_ids(self):
        manager = self.manager()
        with self.assertRaises(ValueError):
            manager.scripts.create({"name": "x", "code": "rm -rf /"})
        with self.assertRaises(ValueError):
            manager.scripts.get("../../etc/passwd")

    def test_reboot_test_requires_confirmation(self):
        manager = self.manager()
        with self.assertRaises(ValueError):
            manager.test_binding("reboot", {})
        control = manager.control
        # A single confirm is only hold progress; the reboot needs a sustained hold.
        self.assertEqual(manager.test_binding("reboot", {"confirm": True})["status"], "holding")
        self.assertEqual(control.calls, [])
        self.assertEqual(manager.test_binding("volume-up", {})["status"], "ok")

    def test_status_reports_bindings_and_last_press(self):
        manager = self.manager()
        manager.test_binding("volume-up", {})
        status = manager.get_status()
        row = next(item for item in status["bindings"] if item["id"] == "volume-up")
        self.assertEqual(row["last_press"]["status"], "ok")
        self.assertEqual(len(status["bindings"]), 5)


class HttpRoutesTests(unittest.TestCase):
    def setUp(self):
        from app.drone_api import Settings, create_server
        from app.mock_data import seed_mock_userdata

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        root = base / "userdata"
        seed_mock_userdata(root)
        self.install_root = base / "drone-app"
        self.user_dir = base / "hotkeygen"
        for target, value in (("app.integrations.media_keys.paths.drone_install_root", self.install_root),
                              ("app.integrations.streamdeck.paths.drone_install_root", self.install_root),
                              ("app.integrations.media_keys.hotkeygen.USER_DIR", self.user_dir)):
            patcher = mock.patch(target, return_value=value) if target.endswith("root") else mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"), "THEMES_ROOT": str(root / "themes"),
            "BATOCERA_CONF_FILE": str(root / "system" / "batocera.conf"),
            "ES_SETTINGS_FILE": str(root / "system" / "configs" / "emulationstation" / "es_settings.cfg"),
            "DRONE_APP_USERNAME": "admin", "DRONE_APP_PASSWORD": "changeme", "HTTPS_PORT": "0", "HTTP_ONLY": "1",
            "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1", "LOG_DIR": str(base / "logs"), "ROM_METADATA_POLL_SECONDS": "0",
            "DRONE_STATE_DATABASE_FILE": str(base / "state.sqlite3"), "DRONE_DEVICE_ID": "media-keys-http-test",
        }
        env_patch = mock.patch.dict(os.environ, env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        try:
            self.server = create_server(Settings.from_env())
        except PermissionError as error:
            self.skipTest(f"Socket bind is not allowed in this environment: {error}")
        self.port = int(self.server.server_address[1])
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=3)
        self.addCleanup(stop)
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/api/auth/login", method="POST",
                                         data=json.dumps({"username": "admin", "password": "changeme"}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            jar = SimpleCookie()
            jar.load(response.headers.get("Set-Cookie"))
        morsel = next(iter(jar.values()))
        self.cookie = f"{morsel.key}={morsel.value}"

    def call(self, path, payload=None, *, cookie=True, headers=None):
        url = f"http://127.0.0.1:{self.port}/v1/api/admin/integrations{path}"
        all_headers = {"Cookie": self.cookie} if cookie else {}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            all_headers["Content-Type"] = "application/json"
        all_headers.update(headers or {})
        request = urllib.request.Request(url, data=data, headers=all_headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            body = error.read()
            return error.code, (json.loads(body) if body[:1] == b"{" else body)

    def test_card_and_status_require_a_real_session(self):
        status, cards = self.call("")
        self.assertEqual(status, 200)
        self.assertIn("media-keys", [card["id"] for card in cards["integrations"]])
        self.assertEqual(self.call("/media-keys/status", cookie=False)[0], 401)
        self.assertEqual(self.call("/media-keys/enable", {}, cookie=False)[0], 401)
        status, payload = self.call("/media-keys/status")
        self.assertEqual((status, payload["enabled"], payload["scope"]), (200, False, "local-only"))
        self.assertEqual(len(payload["bindings"]), 5)

    def test_mutations_need_json_and_same_origin(self):
        self.assertEqual(self.call("/media-keys/enable", {}, headers={"Origin": "http://evil.example"})[0], 403)
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/api/admin/integrations/media-keys/enable",
                                         data=b"{}", headers={"Cookie": self.cookie, "Content-Type": "text/plain"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 415)

    def test_enable_bind_script_disable_end_to_end(self):
        status, result = self.call("/media-keys/enable", {})
        self.assertEqual(status, 200, result)
        self.assertTrue((self.user_dir / "drone-media-keys.conf").is_file())
        status, script = self.call("/media-keys/scripts", {"name": "Hi", "code": "#!/bin/bash\necho hi\n"})
        self.assertEqual(status, 201, script)
        status, bound = self.call("/media-keys/bindings/exit", {"action_type": "script", "script_id": script["id"]})
        self.assertEqual(status, 200, bound)
        self.assertEqual(bound["binding"]["script_name"], "Hi")
        # In use: cannot delete.
        self.assertEqual(self.call(f"/media-keys/scripts/{script['id']}/delete", {})[0], 409)
        # Raw commands and bad IDs are refused.
        self.assertEqual(self.call("/media-keys/bindings/exit", {"action_type": "command", "command": "id"})[0], 400)
        self.assertEqual(self.call("/media-keys/bindings/exit", {"action_type": "script", "script_id": "../x"})[0], 400)
        self.assertEqual(self.call("/media-keys/scripts", {"name": "Raw", "code": "rm -rf /"})[0], 400)
        self.assertEqual(self.call("/media-keys/scripts/not-an-id")[0], 400)
        self.assertEqual(self.call("/media-keys/bindings/reboot/test", {})[0], 400)  # needs confirm
        status, result = self.call("/media-keys/disable", {})
        self.assertEqual(status, 200, result)
        self.assertFalse((self.user_dir / "drone-media-keys.conf").exists())
        status, removed = self.call("/media-keys/remove", {"include_configuration": True})
        self.assertEqual((status, removed["status"]), (200, "removed"))
        self.assertFalse((self.install_root / "integrations" / "media-keys").exists())

    def test_ui_wiring(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/static/js/integrations.js", timeout=5) as response:
            js = response.read().decode("utf-8")
        self.assertIn("async function renderMediaKeysPage()", js)
        self.assertIn("themed-table", js)
        self.assertIn("data-mk-hold", js)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/static/js/drone.js", timeout=5) as response:
            router = response.read().decode("utf-8")
        self.assertIn('hash === "#admin/integrations/media-keys"', router)


if __name__ == "__main__":
    unittest.main()
