"""Adversarial acceptance tests for Issue #142.

The UI is exercised in Node with deterministic state and browser helper stubs;
the backend status test uses an isolated StreamDeckPaths tree and no hardware.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.integrations.streamdeck.manager import StreamDeckIntegration
from app.integrations.streamdeck.paths import StreamDeckPaths, atomic_write_json


ROOT = Path(__file__).resolve().parents[2]


class Issue142UiAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise RuntimeError("node is required by the registered executable UI UAT")
        cls.source = (ROOT / "app/web/static/js/integrations.js").read_text(encoding="utf-8")

    def evaluate(self, scenario):
        # Evaluate the production renderer functions, not a copied UI model.
        setup = r'''
const assert = require('node:assert/strict');
function escapeHtml(v) { return String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;'); }
const document = { addEventListener() {}, getElementById() { return null; }, querySelectorAll() { return []; } };
const window = {};
const titleNode = {}, subtitleNode = {}, content = {};
'''
        check = r'''
sdState.status = fixture.status;
sdState.profilesPayload = fixture.profiles;
sdState.selectedDeviceId = fixture.selectedDeviceId || '';
const buttons = sdButtonsHtml();
const settings = sdDeviceSettingsHtml();
const diagnostics = sdDiagnosticsHtml();
assert.match(buttons, /data-sd-action="profile-add"/);
assert.match(buttons, /Fixture Profile/);
assert.match(buttons, /Automatic profile switching/);
if (fixture.expectEmpty) {
  assert.match(buttons, /No Stream Deck connected/);
  assert.doesNotMatch(buttons, /class="sd-deck"/);
  assert.doesNotMatch(buttons, /data-sd-action="edit-key"/);
  assert.doesNotMatch(buttons, /sdShowRendered/);
  assert.doesNotMatch(diagnostics, /data-sd-action="identify"/);
  assert.doesNotMatch(diagnostics, /id="sdTestKey"/);
  assert.doesNotMatch(diagnostics, /data-sd-action="test-key"/);
  assert.match(settings, /No Stream Deck connected/);
  assert.doesNotMatch(settings, /id="sdBrightness"/);
} else {
  assert.match(buttons, /class="sd-deck"/);
  assert.match(buttons, /data-sd-action="edit-key"/);
  assert.match(settings, /id="sdBrightness"/);
  assert.match(buttons, /sdShowRendered/);
  assert.match(diagnostics, /data-sd-action="identify"/);
  assert.match(diagnostics, /id="sdTestKey"/);
}
'''
        fixture = {
            "status": {"devices": scenario["devices"]},
            "profiles": {"profiles": [{"id": "fixture", "name": "Fixture Profile", "buttons": [{"key": 8, "label": "Configured", "action_type": "none"}]}], "context_rules": []},
            "expectEmpty": scenario["expect_empty"],
            "selectedDeviceId": scenario.get("selected_id", ""),
        }
        script = setup + self.source + "\nconst fixture = " + json.dumps(fixture) + ";\n" + check
        result = subprocess.run([self.node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_remembered_only_and_stale_selection_show_empty_device_state_but_keep_profiles(self):
        remembered = {"id": "old-mini", "model": "Stream Deck Mini", "serial": "old-mini", "rows": 2, "columns": 3, "key_count": 6, "connected": False, "source": "remembered"}
        self.evaluate({"devices": [remembered], "selected_id": "old-mini", "expect_empty": True})

    def test_no_devices_has_no_six_key_placeholder_even_when_profile_uses_high_key(self):
        self.evaluate({"devices": [], "expect_empty": True})

    def test_attached_deck_keeps_its_real_layout_and_device_settings(self):
        attached = {"id": "live-mini", "model": "Stream Deck Mini", "serial": "live-mini", "rows": 2, "columns": 3, "key_count": 6, "connected": True, "source": "runtime"}
        self.evaluate({"devices": [attached], "expect_empty": False})


class Issue142StatusAcceptanceTests(unittest.TestCase):
    def test_remembered_layout_is_not_reported_as_connected_or_layout_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = StreamDeckPaths(root / "integrations" / "streamdeck")
            paths.ensure()
            atomic_write_json(paths.state_dir / "known-devices.json", {
                "old-mini": {"id": "old-mini", "model": "Stream Deck Mini", "serial": "old-mini", "rows": 2, "columns": 3, "key_count": 6}
            })
            env = {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"), "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"), "DRONE_DEVICE_ID": "issue-142-adversarial", "LOG_DIR": str(root / "logs")}
            with mock.patch.dict(os.environ, env, clear=True):
                manager = StreamDeckIntegration(SimpleNamespace(roms_root=root / "roms"), None, paths=paths, usb_detector=lambda: [])
            status = manager.get_status()
            self.assertEqual(status["connected_device_count"], 0)
            self.assertEqual(status["device"], "disconnected")
            self.assertFalse(status["layout_available"])
            self.assertEqual([(row["source"], row["connected"], row["rows"], row["columns"]) for row in status["devices"]], [("remembered", False, 2, 3)])


if __name__ == "__main__":
    unittest.main()
