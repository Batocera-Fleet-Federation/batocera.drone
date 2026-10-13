"""Adversarial acceptance checks for Issue #152 Media Keys safety gates.

All paths and clocks are isolated; the built-in executor is a deterministic fake.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.integrations.media_keys.config import ConfigStore
from app.integrations.media_keys.dispatch import HoldGate, handle_press
from app.integrations.media_keys.manager import MediaKeysIntegration
from app.integrations.media_keys.paths import MediaKeysPaths


class RecordingControl:
    def __init__(self):
        self.calls = []

    def run_builtin(self, action_id):
        self.calls.append(action_id)
        return {"status": "ok"}


class Issue152SafetyAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = MediaKeysPaths(Path(self.tmp.name) / "integrations" / "media-keys")
        self.paths.ensure_layout()
        self.control = RecordingControl()
        self.manager = MediaKeysIntegration(mock.Mock(), mock.Mock(), paths=self.paths,
                                            control=self.control, keyboard_detector=lambda: [])

    def test_reboot_test_action_cannot_be_triggered_by_a_single_confirm_flag(self):
        """The issue requires a hold before reboot/shutdown, including Test controls."""
        result = self.manager.test_binding("reboot", {"confirm": True})
        self.assertNotEqual(result.get("status"), "ok", "one API request bypassed the required hold")
        self.assertEqual(self.control.calls, [], "reboot action ran without a sustained hold")

    def test_malformed_persisted_hold_state_fails_closed_and_recovers(self):
        """Corrupt state must not permanently brick a key or accidentally satisfy a hold."""
        self.paths.hold_file.write_text('{"reboot":{"start":"NaN-or-text","last":100,"fired":false}}')
        now = [100.0]
        gate = HoldGate(self.paths, clock=lambda: now[0])
        outcome = gate.check("reboot", 3.0)
        self.assertEqual(outcome, "holding")
        now[0] += 0.5
        self.assertEqual(gate.check("reboot", 3.0), "holding")

    def test_early_release_gap_restarts_confirmation_window(self):
        ConfigStore(self.paths).set_enabled(True)
        now = [500.0]
        clock = lambda: now[0]
        self.assertEqual(handle_press("reboot", self.paths, control=self.control, clock=clock)["status"], "holding")
        now[0] += 1.01
        self.assertEqual(handle_press("reboot", self.paths, control=self.control, clock=clock)["status"], "holding")
        self.assertEqual(self.control.calls, [])


if __name__ == "__main__":
    unittest.main()
