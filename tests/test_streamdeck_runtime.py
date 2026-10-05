"""Stream Deck hardware runtime tests with mock hardware (no StreamDeck library).

Covers device discovery/hotplug, key-down triggering, debounce, overlap
protection, hold-to-confirm, profile navigation and context rules, diagnostics
commands, render-failure isolation, config reload, and -- when Pillow is
available -- the real worker process end to end against a fake ``StreamDeck``
package placed in the integration's private ``lib/``.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.integrations.streamdeck.actions import BatoceraControl, BuiltInActionRegistry, RetroArchControl
from app.integrations.streamdeck.devices import (
    FakeDeviceProvider, FakeStreamDeckDevice, detect_usb_devices, stable_device_id, usb_signature,
)
from app.integrations.streamdeck.game_runtime import GameRuntime
from app.integrations.streamdeck.logs import set_log_sink
from app.integrations.streamdeck.paths import StreamDeckPaths, atomic_write_json, read_json, try_file_lock
from app.integrations.streamdeck.runtime import StreamDeckRuntime

try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:  # pragma: no cover
    HAVE_PIL = False

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTimer:
    def __init__(self, interval, function, args=(), kwargs=None) -> None:
        self.interval, self.function, self.args = interval, function, args
        self.cancelled = False
        self.started = False
        self.daemon = True

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.function(*self.args)


class FakeRenderer:
    def __init__(self) -> None:
        self.calls = []
        self.previews = []

    def render(self, spec, size):
        if spec.get("text") == "BOOM":
            raise ValueError("unreadable artwork")
        self.calls.append((dict(spec), tuple(size)))
        return ("image", spec.get("kind"), spec.get("text"), tuple(size))

    def write_preview(self, image, path) -> None:
        self.previews.append(Path(path))


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls = []
        self.coordinator = SimpleNamespace(exit_timeout=20.0, start_timeout=45.0)

    def execute(self, button, context, navigate=None):
        self.calls.append((button, context))
        if button.get("action_type") == "profile":
            return navigate(button)
        return {"status": "ok"}


def _builtin(key, action_id):
    return {"key": key, "action_type": "builtin", "action_id": action_id,
            "render": {"kind": "generated", "text": action_id.upper()}}


def _document(generation=1, **overrides):
    document = {
        "generation": generation,
        "settings": {"confirm_dangerous_actions": True, "hold_duration_ms": 1500, "exit_timeout_seconds": 20,
                     "launch_confirm_timeout_seconds": 45, "script_timeout_seconds": 30},
        "default_profile_id": "default",
        "profiles": [
            {"id": "default", "name": "Default", "buttons": [
                _builtin(0, "exit-game"),
                _builtin(1, "reboot-system"),
                {"key": 2, "action_type": "profile", "operation": "next", "render": {"kind": "generated", "text": "NEXT"}},
                {"key": 3, "action_type": "script", "script_id": "a" * 32, "render": {"kind": "generated", "text": "BOOM"}},
            ]},
            {"id": "games", "name": "Games", "buttons": [
                {"key": 0, "action_type": "profile", "operation": "go-to", "profile_id": "default",
                 "render": {"kind": "generated", "text": "HOME"}},
            ]},
        ],
        "devices": [{"device_id": "FAKE0001", "brightness": 35, "startup_profile_id": ""}],
        "context_rules": [],
    }
    document.update(overrides)
    return document


class RuntimeHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = []
        set_log_sink(self.logs.append)
        self.addCleanup(set_log_sink, None)
        self.paths = StreamDeckPaths(Path(self.tmp.name) / "integrations" / "streamdeck")
        self.paths.ensure_layout()
        self.clock = FakeClock()
        self.timers = []
        self.renderer = FakeRenderer()
        self.dispatcher = RecordingDispatcher()
        self.active_game = None
        self.game_runtime = GameRuntime(detector=lambda: self.active_game, sleep=lambda _s: None)
        control = BatoceraControl(which=lambda name: f"/usr/bin/{name}")
        self.actions = BuiltInActionRegistry(control, RetroArchControl(proc_root=Path(self.tmp.name) / "noproc"), self.game_runtime)
        self.provider = FakeDeviceProvider()
        self.statuses = []
        atomic_write_json(self.paths.runtime_document, _document())

        def timer_factory(interval, function, args=(), kwargs=None):
            timer = FakeTimer(interval, function, args)
            self.timers.append(timer)
            return timer

        self.runtime = StreamDeckRuntime(
            self.paths, self.provider, self.renderer, self.dispatcher, self.actions, self.game_runtime,
            clock=self.clock, spawn=lambda fn, _name: fn(), timer_factory=timer_factory,
            usb_probe=lambda: None, status_writer=self.statuses.append,
        )
        self.runtime.load_document(force=True)

    def deck(self, serial="FAKE0001", rows=2, columns=3, size=(80, 80)) -> FakeStreamDeckDevice:
        device = FakeStreamDeckDevice(serial=serial, rows=rows, columns=columns, key_image_size=size)
        self.provider.devices.append(device)
        return device

    def reconcile(self) -> None:
        self.clock.advance(31)
        self.runtime.reconcile()


class DeviceDiscoveryTests(RuntimeHarness):
    def test_no_device_connected_keeps_runtime_healthy(self):
        self.reconcile()
        self.assertEqual(self.runtime.slots, {})
        self.assertEqual(self.statuses[-1]["devices"], [])
        self.assertEqual(self.statuses[-1]["runtime"], "running")

    def test_one_device_is_opened_reset_rendered_and_dimmed(self):
        device = self.deck()
        self.reconcile()
        self.assertTrue(device.is_open)
        self.assertEqual(device.reset_count, 1)
        self.assertEqual(device.brightness, 35)
        self.assertEqual(sorted(device.images), list(range(6)))
        self.assertEqual(device.images[0], ("native", "Stream Deck Mini", ("image", "generated", "EXIT-GAME", (80, 80))))
        status = self.statuses[-1]["devices"][0]
        self.assertEqual((status["id"], status["key_count"], status["rows"], status["columns"]), ("FAKE0001", 6, 2, 3))
        self.assertTrue(any("event=device-attach" in line for line in self.logs))

    def test_multiple_devices_use_their_own_geometry(self):
        mini = self.deck()
        xl = self.deck(serial="XL0001", rows=4, columns=8, size=(96, 96))
        self.reconcile()
        self.assertEqual(len(self.runtime.slots), 2)
        self.assertEqual(len(mini.images), 6)
        self.assertEqual(len(xl.images), 32)
        self.assertIn(((96, 96)), {size for _spec, size in self.renderer.calls})

    def test_device_that_fails_to_open_is_isolated_and_retried_later(self):
        broken = self.deck(serial="BROKEN")
        good = self.deck(serial="GOOD")
        broken.open = mock.Mock(side_effect=OSError("permission denied"))
        self.reconcile()
        self.assertEqual([slot.device_id for slot in self.runtime.slots.values()], ["GOOD"])
        self.assertTrue(good.is_open)
        # GOOD attaching (and its own key-4 render error) must not hide BROKEN's problem.
        self.assertTrue(any("permission denied" in error for error in self.runtime.status["errors"]))
        broken.open.reset_mock()
        self.clock.advance(1)
        self.runtime.reconcile(force=False)
        broken.open.assert_not_called()  # backoff, no log spam
        errors = [line for line in self.logs if "permission denied" in line]
        self.assertEqual(len(errors), 1)
        del broken.open  # the real open() works again, e.g. udev permissions fixed
        self.clock.advance(31)
        self.runtime.reconcile()
        self.assertEqual(len(self.runtime.slots), 2)
        self.assertFalse(any("permission denied" in error for error in self.runtime.status["errors"]))

    def test_disconnect_then_reconnect(self):
        device = self.deck()
        self.reconcile()
        device.unplug()
        self.reconcile()
        self.assertEqual(self.runtime.slots, {})
        self.assertTrue(any("event=device-detach" in line for line in self.logs))
        device.plug_in()
        self.reconcile()
        self.assertEqual(len(self.runtime.slots), 1)
        self.assertTrue(any("event=device-reconnect" in line for line in self.logs))
        self.assertEqual(device.open_count, 2)

    def test_write_failure_detaches_device_for_reattach(self):
        device = self.deck()
        self.reconcile()
        device.fail_writes = True
        self.runtime.load_document(force=True)
        self.assertEqual(self.runtime.slots, {})
        device.fail_writes = False
        self.reconcile()
        self.assertEqual(len(self.runtime.slots), 1)

    def test_deck_without_key_screens_is_supported_without_images(self):
        pedal = self.deck(serial="PEDAL1", rows=1, columns=3, size=(0, 0))
        self.reconcile()
        self.assertEqual(len(self.runtime.slots), 1)
        self.assertEqual(pedal.images, {})
        self.assertFalse(self.statuses[-1]["devices"][0]["has_key_images"])

    def test_invalid_device_is_isolated_from_supported_ones(self):
        bad = self.deck(serial="BAD")
        bad.get_capabilities = mock.Mock(side_effect=RuntimeError("unsupported HID report"))
        self.deck(serial="GOOD")
        self.reconcile()
        self.assertEqual([slot.device_id for slot in self.runtime.slots.values()], ["GOOD"])
        self.assertFalse(bad.is_open)
        self.assertTrue(any("unsupported HID report" in error for error in self.runtime.status["errors"]))

    def test_reload_reports_devices_that_could_not_be_updated(self):
        device = self.deck()
        self.reconcile()
        device.fail_writes = True
        atomic_write_json(self.paths.commands_dir / "1.json", {"id": "r1", "op": "reload"})
        self.runtime.process_commands()
        result = read_json(self.paths.results_dir / "r1.json")
        self.assertEqual((result["status"], result["failed_devices"]), ("error", ["FAKE0001"]))

    def test_unrenderable_key_shows_error_art_without_blanking_the_deck(self):
        device = self.deck()
        self.reconcile()
        self.assertEqual(device.images[3][2][2], "ERR")
        self.assertEqual(device.images[0][2][2], "EXIT-GAME")
        self.assertTrue(self.runtime.slots)


class KeyInputTests(RuntimeHarness):
    def setUp(self) -> None:
        super().setUp()
        self.device = self.deck()
        self.reconcile()

    def test_key_down_triggers_once_and_key_up_does_not(self):
        self.device.key_down(0)
        self.device.key_up(0)
        self.assertEqual(len(self.dispatcher.calls), 1)
        button, context = self.dispatcher.calls[0]
        self.assertEqual(button["action_id"], "exit-game")
        self.assertEqual((context.key_index, context.device_id, context.trigger), (0, "FAKE0001", "button"))
        self.assertEqual(self.statuses[-1]["last_action"]["action_id"], "exit-game")

    def test_duplicate_key_down_and_bounce_are_ignored(self):
        self.device.key_down(0)
        self.device.key_down(0)  # duplicate callback without key-up
        self.device.key_up(0)
        self.clock.advance(0.05)
        self.device.press(0)  # bounce inside debounce window
        self.assertEqual(len(self.dispatcher.calls), 1)
        self.clock.advance(1)
        self.device.press(0)
        self.assertEqual(len(self.dispatcher.calls), 2)

    def test_key_with_running_action_ignores_new_presses(self):
        slot = next(iter(self.runtime.slots.values()))
        slot.busy.add(0)
        self.clock.advance(1)
        self.device.press(0)
        self.assertEqual(self.dispatcher.calls, [])
        self.assertTrue(any("event=button-ignored" in line for line in self.logs))

    def test_dangerous_action_released_early_is_cancelled(self):
        self.device.key_down(1)
        self.assertEqual(self.dispatcher.calls, [])
        self.assertEqual(self.device.images[1][2][2], "HOLD")
        self.device.key_up(1)
        self.assertTrue(self.timers[-1].cancelled)
        self.timers[-1].fire()
        self.assertEqual(self.dispatcher.calls, [])
        self.assertEqual(self.statuses[-1]["last_action_result"]["status"], "cancelled")
        self.assertEqual(self.device.images[1][2][2], "REBOOT-SYSTEM")

    def test_dangerous_action_held_for_hold_time_runs_confirmed(self):
        self.device.key_down(1)
        timer = self.timers[-1]
        self.assertEqual(timer.interval, 1.5)
        timer.fire()
        self.assertEqual(len(self.dispatcher.calls), 1)
        self.assertTrue(self.dispatcher.calls[0][1].confirmed)
        self.device.key_up(1)
        self.assertEqual(len(self.dispatcher.calls), 1)

    def test_dangerous_safeguard_can_be_disabled(self):
        document = _document(generation=2)
        document["settings"]["confirm_dangerous_actions"] = False
        atomic_write_json(self.paths.runtime_document, document)
        self.runtime.load_document()
        self.clock.advance(1)
        self.device.key_down(1)
        self.assertEqual(len(self.dispatcher.calls), 1)
        self.assertTrue(self.dispatcher.calls[0][1].confirmed)

    def test_profile_navigation_next_and_go_to(self):
        self.device.press(2)
        slot = next(iter(self.runtime.slots.values()))
        self.assertEqual(slot.active_profile_id, "games")
        self.assertEqual(self.device.images[0][2][2], "HOME")
        self.assertEqual(self.device.images[1][2], ("image", "blank", None, (80, 80)))
        self.clock.advance(1)
        self.device.press(0)
        self.assertEqual(slot.active_profile_id, "default")
        self.assertTrue(any("event=profile-change" in line for line in self.logs))

    def test_config_reload_reapplies_without_restart(self):
        document = _document(generation=5)
        document["profiles"][0]["buttons"][0]["render"] = {"kind": "generated", "text": "NEW"}
        atomic_write_json(self.paths.runtime_document, document)
        self.assertTrue(self.runtime.load_document())
        self.assertEqual(self.device.images[0][2][2], "NEW")
        self.assertEqual(self.runtime.status["applied_generation"], 5)
        self.assertFalse(self.runtime.load_document())  # same generation: nothing to do

    def test_context_rule_switches_profile_on_game_start_and_back_on_stop(self):
        document = _document(generation=3, context_rules=[
            {"id": "r1", "enabled": True, "event": "game-start", "system": "snes", "emulator": "", "profile_id": "games"},
            {"id": "r2", "enabled": True, "event": "game-stop", "system": "", "emulator": "", "profile_id": "default"},
        ])
        atomic_write_json(self.paths.runtime_document, document)
        self.runtime.load_document()
        slot = next(iter(self.runtime.slots.values()))
        self.active_game = {"system_name": "snes", "rom_path": "/roms/snes/a.sfc", "cmdline": "emulatorlauncher -system snes -rom /roms/snes/a.sfc", "pid": 5}
        self.runtime.tick()
        self.assertEqual(slot.active_profile_id, "games")
        self.active_game = None
        self.clock.advance(5)
        self.runtime.tick()
        self.assertEqual(slot.active_profile_id, "default")


class CommandTests(RuntimeHarness):
    def setUp(self) -> None:
        super().setUp()
        self.device = self.deck()
        self.reconcile()

    def _command(self, **payload):
        atomic_write_json(self.paths.commands_dir / f"{time.time_ns()}.json", {"id": "c1", **payload})
        self.runtime.process_commands()
        return read_json(self.paths.results_dir / "c1.json")

    def test_identify_shows_key_numbers_then_restores(self):
        result = self._command(op="identify", device_id="")
        self.assertEqual(result["status"], "ok")
        self.assertEqual([self.device.images[key][2][2] for key in range(6)], ["1", "2", "3", "4", "5", "6"])
        self.timers[-1].fire()
        self.assertEqual(self.device.images[0][2][2], "EXIT-GAME")

    def test_test_button_flashes_without_running_action(self):
        result = self._command(op="test-button", device_id="FAKE0001", key=0)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["action_executed"])
        self.assertEqual(self.device.images[0][2][2], "TEST")
        self.assertEqual(self.dispatcher.calls, [])
        self.timers[-1].fire()
        self.assertEqual(self.device.images[0][2][2], "EXIT-GAME")

    def test_test_connection_reports_capabilities_and_communication(self):
        result = self._command(op="test-connection", device_id="")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["devices"][0]["communication"], "ok")
        self.assertEqual(result["devices"][0]["capabilities"]["key_count"], 6)

    def test_unknown_commands_are_ignored(self):
        atomic_write_json(self.paths.commands_dir / "x.json", {"id": "c2", "op": "rm -rf"})
        self.runtime.process_commands()
        self.assertIsNone(read_json(self.paths.results_dir / "c2.json"))

    def test_shutdown_releases_devices(self):
        self.runtime.shutdown()
        self.assertFalse(self.device.is_open)
        self.assertEqual(self.statuses[-1]["runtime"], "stopped")


class UsbDetectionTests(unittest.TestCase):
    def test_sysfs_detection_and_signature(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name, vendor, product, serial in (("7-1", "0fd9", "0063", "AL12345"), ("1-2", "046d", "c52b", "X"),
                                                   ("3-4", "0fd9", "0fff", "")):
                (root / name).mkdir()
                (root / name / "idVendor").write_text(vendor + "\n")
                (root / name / "idProduct").write_text(product + "\n")
                (root / name / "serial").write_text(serial + "\n")
            devices = detect_usb_devices(root)
            self.assertEqual([row["usb_id"] for row in devices], ["0fd9:0fff", "0fd9:0063"])
            mini = devices[1]
            self.assertEqual((mini["model"], mini["key_count"], mini["id"]), ("Stream Deck Mini", 6, "AL12345"))
            self.assertFalse(devices[0]["known_model"])
            self.assertTrue(usb_signature(root))
            self.assertIsNone(usb_signature(root / "missing"))

    def test_stable_device_id_hashes_unsafe_serials(self):
        self.assertEqual(stable_device_id("ABC123", "Mini"), "ABC123")
        unsafe = stable_device_id("../../etc", "Mini", "path")
        self.assertTrue(unsafe.startswith("deck-"))
        self.assertEqual(unsafe, stable_device_id("../../etc", "Mini", "path"))


FAKE_STREAMDECK_LIBRARY = {
    "StreamDeck/__init__.py": "",
    "StreamDeck/ImageHelpers/__init__.py": "",
    "StreamDeck/ImageHelpers/PILHelper.py": textwrap.dedent('''
        def to_native_format(deck, image):
            return image.tobytes()
    '''),
    "StreamDeck/DeviceManager.py": textwrap.dedent('''
        import json, os

        class FakeDeck:
            def __init__(self):
                self._open = False
                self._log = os.environ.get("FAKE_DECK_LOG")
            def _record(self, event, **values):
                with open(self._log, "a") as handle:
                    handle.write(json.dumps({"event": event, **values}) + "\\n")
            def id(self): return "/dev/hidraw-fake"
            def deck_type(self): return "Stream Deck Mini"
            def key_count(self): return 6
            def key_layout(self): return (2, 3)
            def key_image_format(self): return {"size": (80, 80), "format": "BMP", "flip": (False, True), "rotation": 90}
            def is_visual(self): return True
            def open(self): self._open = True; self._record("open")
            def close(self): self._open = False; self._record("close")
            def reset(self): self._record("reset")
            def connected(self): return True
            def get_serial_number(self): return "FAKESERIAL1"
            def get_firmware_version(self): return "3.00"
            def set_brightness(self, value): self._record("brightness", value=value)
            def set_key_image(self, key, image): self._record("image", key=key, size=len(image or b""))
            def set_key_callback(self, callback): self._record("callback", set=callback is not None)

        class DeviceManager:
            def enumerate(self):
                return [FakeDeck()]
    '''),
}


@unittest.skipUnless(HAVE_PIL and hasattr(os, "fork"), "needs Pillow and POSIX processes")
class WorkerProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = StreamDeckPaths(Path(self.tmp.name) / "integrations" / "streamdeck")
        self.paths.ensure_layout()
        for relative, source in FAKE_STREAMDECK_LIBRARY.items():
            target = self.paths.lib_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
        self.roms = Path(self.tmp.name) / "roms"
        self.roms.mkdir()
        self.deck_log = Path(self.tmp.name) / "deck.jsonl"
        self.env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "FAKE_DECK_LOG": str(self.deck_log)}
        # The fake library reports serial FAKESERIAL1; its saved brightness must be applied.
        atomic_write_json(self.paths.runtime_document, _document(devices=[
            {"device_id": "FAKESERIAL1", "brightness": 35, "startup_profile_id": ""}]))

    def _events(self):
        if not self.deck_log.exists():
            return []
        return [json.loads(line) for line in self.deck_log.read_text().splitlines() if line.strip()]

    def _wait(self, predicate, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def test_worker_runs_end_to_end_with_fake_library_and_releases_device(self):
        command = [sys.executable, "-m", "app.integrations.streamdeck.worker", "--root", str(self.paths.root),
                   "--roms-root", str(self.roms), "--parent-pid", str(os.getpid())]
        process = subprocess.Popen(command, cwd=str(REPO_ROOT), env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            self.assertTrue(self._wait(lambda: (read_json(self.paths.worker_status_file, {}) or {}).get("devices")),
                            "worker never reported the device")
            status = read_json(self.paths.worker_status_file, {})
            self.assertEqual(status["devices"][0]["id"], "FAKESERIAL1")
            self.assertEqual(status["devices"][0]["key_count"], 6)
            self.assertTrue(self._wait(lambda: len({e["key"] for e in self._events() if e["event"] == "image"}) == 6))
            self.assertIn({"event": "brightness", "value": 35}, self._events())
            self.assertTrue((self.paths.preview_dir / "FAKESERIAL1" / "0.png").is_file())
            self.assertTrue(any(self.paths.rendered_dir.glob("*.png")))
            # Duplicate prevention: a second worker exits immediately.
            duplicate = subprocess.run(command + ["--lock-wait", "0.2"], cwd=str(REPO_ROOT), env=self.env,
                                       capture_output=True, timeout=30)
            self.assertEqual(duplicate.returncode, 3)
            # Identify through the command spool.
            atomic_write_json(self.paths.commands_dir / "1.json", {"id": "abc", "op": "identify", "device_id": ""})
            self.assertTrue(self._wait(lambda: read_json(self.paths.results_dir / "abc.json") is not None))
            self.assertEqual(read_json(self.paths.results_dir / "abc.json")["status"], "ok")
        finally:
            process.terminate()
            process.wait(timeout=20)
        events = [event["event"] for event in self._events()]
        self.assertEqual(events[-1], "close")
        self.assertEqual(read_json(self.paths.worker_status_file, {})["runtime"], "stopped")
        self.assertFalse(self.paths.pid_file.exists())
        log = self.paths.runtime_log.read_text()
        self.assertIn("event=runtime-started", log)
        self.assertIn("event=device-attach", log)

    def test_worker_exits_when_lock_is_held_by_another_runtime(self):
        with try_file_lock(self.paths.runtime_lock) as acquired:
            self.assertTrue(acquired)
            result = subprocess.run(
                [sys.executable, "-m", "app.integrations.streamdeck.worker", "--root", str(self.paths.root),
                 "--roms-root", str(self.roms), "--lock-wait", "0.2"],
                cwd=str(REPO_ROOT), env=self.env, capture_output=True, timeout=30,
            )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self._events(), [])


if __name__ == "__main__":
    unittest.main()
