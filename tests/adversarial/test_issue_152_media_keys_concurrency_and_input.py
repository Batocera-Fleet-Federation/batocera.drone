"""Issue #152 boundaries: concurrent key-repeat dispatchers and hostile binding input.

hotkeygen starts one dispatcher process per key event, so a held reboot key means
many dispatchers racing on the shared hold state; the destructive action must still
fire exactly once. Binding input arrives as JSON (which Python parses NaN/Infinity
from), so numeric edge cases must be refused rather than disabling the hold gate.
"""

import json
import math
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.integrations.media_keys import dispatch as dispatch_module
from app.integrations.media_keys.config import ConfigStore, hold_seconds, normalize_config, validate_binding
from app.integrations.media_keys.dispatch import HoldGate, handle_press
from app.integrations.media_keys.paths import MediaKeysPaths


class RecordingControl:
    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def run_builtin(self, action_id):
        with self._lock:
            self.calls.append(action_id)
        return {"status": "ok"}


class ConcurrentRepeatTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = MediaKeysPaths(Path(tmp.name).resolve() / "integrations" / "media-keys")
        self.paths.ensure_layout()
        ConfigStore(self.paths).set_enabled(True)
        patcher = mock.patch.object(dispatch_module, "_drone_log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_concurrent_repeats_of_a_sustained_hold_fire_reboot_once(self):
        control = RecordingControl()
        now = [500.0]
        clock_lock = threading.Lock()

        def clock():
            with clock_lock:
                return now[0]

        # Build up a genuine typematic train serially (stopping short of the 3 s
        # default hold), then release a burst of simultaneous repeats just past it.
        offsets = [0.0, 0.5] + [0.5 + 0.033 * n for n in range(1, 75)]
        for offset in offsets:
            now[0] = 500.0 + offset
            handle_press("reboot", self.paths, control=control, clock=clock)
        self.assertEqual(control.calls, [], "hold not yet long enough")
        self.assertLess(offsets[-1], 3.0)
        now[0] = 500.0 + 3.0 + 0.01
        threads = [threading.Thread(target=handle_press, args=("reboot", self.paths),
                                    kwargs={"control": control, "clock": clock}) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(control.calls, ["reboot"], "concurrent repeats must fire the destructive action exactly once")

    def test_concurrent_first_presses_never_fire(self):
        control = RecordingControl()
        threads = [threading.Thread(target=handle_press, args=("reboot", self.paths),
                                    kwargs={"control": control, "clock": lambda: 900.0}) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(control.calls, [])

    def test_hold_state_stays_valid_json_under_contention(self):
        gate = HoldGate(self.paths, clock=lambda: 10.0)
        threads = [threading.Thread(target=gate.check, args=("reboot", 3.0)) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertIsInstance(json.loads(self.paths.hold_file.read_text()), dict)


class HostileBindingInputTests(unittest.TestCase):
    def test_non_finite_and_negative_hold_seconds_are_refused(self):
        for bad in (float("nan"), float("inf"), float("-inf"), -1, True, "3", None.__class__):
            with self.subTest(hold=bad), self.assertRaises(ValueError):
                validate_binding("reboot", {"action_type": "builtin", "action_id": "reboot", "hold_seconds": bad})

    def test_json_parsed_nan_cannot_remove_the_reboot_hold(self):
        raw = json.loads('{"action_type": "builtin", "action_id": "reboot", "hold_seconds": NaN}')
        with self.assertRaises(ValueError):
            validate_binding("reboot", raw)

    def test_stored_config_with_non_finite_or_tiny_reboot_hold_is_normalized_safe(self):
        for stored in ('NaN', 'Infinity', '0', '0.001', '-5', '"x"'):
            with self.subTest(stored=stored):
                raw = json.loads('{"enabled": true, "bindings": {"reboot": {"action_type": "builtin", '
                                 '"action_id": "reboot", "hold_seconds": %s}}}' % stored)
                binding = normalize_config(raw)["bindings"]["reboot"]
                seconds = hold_seconds(binding)
                self.assertTrue(math.isfinite(seconds) and seconds >= 2.0, (stored, binding))
                self.assertEqual(binding["action_id"], "reboot")

    def test_hold_seconds_on_none_binding_is_ignored_and_other_types_refused(self):
        self.assertEqual(validate_binding("exit", {"action_type": "none", "hold_seconds": 5}), {"action_type": "none"})
        for raw in ({"action_type": ["builtin"]}, {"action_type": "builtin", "action_id": ["reboot"]},
                    {"action_type": "script", "script_id": 12}, {"action_type": "script", "script_id": "A" * 31}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_binding("exit", raw)


if __name__ == "__main__":
    unittest.main()
