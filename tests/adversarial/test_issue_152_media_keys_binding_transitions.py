"""Issue #152 acceptance: a hold confirms the current dangerous binding only.

All presses use a fake clock and executor. No hardware or reboot command runs.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.integrations.media_keys.config import ConfigStore
from app.integrations.media_keys.dispatch import handle_press
from app.integrations.media_keys.paths import MediaKeysPaths


class RecordingControl:
    def __init__(self):
        self.calls = []

    def run_builtin(self, action_id):
        self.calls.append(action_id)
        return {"status": "ok"}


class BindingTransitionAcceptanceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = MediaKeysPaths(Path(tmp.name).resolve() / "integrations" / "media-keys")
        self.paths.ensure_layout()
        self.config = ConfigStore(self.paths)
        self.config.set_enabled(True)
        self.control = RecordingControl()
        self.now = [1000.0]
        log_patch = mock.patch("app.integrations.media_keys.dispatch._drone_log")
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def press(self, key):
        return handle_press(key, self.paths, control=self.control, clock=lambda: self.now[0])["status"]

    def advance_hold(self, key, *, end=2.9):
        self.assertEqual(self.press(key), "holding")
        self.now[0] += 0.6
        self.assertEqual(self.press(key), "holding")
        while self.now[0] < 1000.0 + end - 0.01:
            self.now[0] += 0.1
            self.assertEqual(self.press(key), "holding")

    def test_shutdown_assigned_to_an_ordinary_key_requires_its_own_hold(self):
        self.config.set_binding("volume-up", {"action_type": "builtin", "action_id": "shutdown"})
        self.assertEqual(self.press("volume-up"), "holding")
        self.assertEqual(self.control.calls, [])
        self.now[0] += 4.0  # A fresh tap after release cannot complete the first hold.
        self.assertEqual(self.press("volume-up"), "holding")
        self.assertEqual(self.control.calls, [])

    def test_reassigning_reboot_to_shutdown_cannot_reuse_reboot_hold_progress(self):
        self.advance_hold("reboot")
        self.config.set_binding("reboot", {"action_type": "builtin", "action_id": "shutdown"})
        self.now[0] += 0.2
        self.assertEqual(self.press("reboot"), "holding")
        self.assertEqual(self.control.calls, [], "the new shutdown action inherited an old reboot hold")

    def test_disabling_and_reenabling_cannot_reuse_a_prior_hold(self):
        self.advance_hold("reboot")
        self.config.set_enabled(False)
        self.now[0] += 0.1
        self.assertEqual(self.press("reboot"), "disabled")
        self.config.set_enabled(True)
        self.now[0] += 0.1
        self.assertEqual(self.press("reboot"), "holding")
        self.assertEqual(self.control.calls, [], "reboot inherited confirmation from before disable")


if __name__ == "__main__":
    unittest.main()
