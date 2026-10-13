"""Issue #155: root-dispatched Media Keys state remains usable by Drone."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.integrations.media_keys.dispatch import HoldGate, record_press
from app.integrations.media_keys.manager import MediaKeysIntegration
from app.integrations.media_keys.paths import MediaKeysPaths


class MediaKeysStateOwnershipAcceptanceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = MediaKeysPaths(Path(tmp.name) / "integrations" / "media-keys")
        self.paths.ensure_layout()
        self.owner = (os.getuid(), os.getgid())

    def test_dispatch_state_is_owned_and_readable_by_service_user(self):
        """The Drone user must read press results and continue the hold state."""
        with mock.patch("app.integrations.media_keys.dispatch.script_owner", return_value=self.owner):
            gate = HoldGate(self.paths, clock=lambda: 100.0)
            self.assertEqual(gate.check("reboot", 2.0), "holding")
            record_press(self.paths, "volume-up", {"status": "ok", "error": ""},
                         clock=lambda: 100.0)

        for path in (self.paths.state_dir, self.paths.state_dir / "hold.lock",
                     self.paths.hold_file, self.paths.press_file):
            with self.subTest(path=path.name):
                self.assertEqual(path.stat().st_uid, self.owner[0])
        self.assertEqual(self.paths.hold_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.paths.press_file.stat().st_mode & 0o777, 0o600)

        # The same service identity can read the UI data and acquire the lock on
        # its next admin hold pulse, even though the files are private (0600).
        manager = MediaKeysIntegration(mock.Mock(), mock.Mock(), paths=self.paths,
                                       keyboard_detector=lambda: [])
        bindings = manager.bindings_view()
        volume = next(row for row in bindings if row["id"] == "volume-up")
        self.assertEqual(volume["last_press"]["status"], "ok")
        self.assertEqual(HoldGate(self.paths, clock=lambda: 100.2).check("reboot", 2.0), "holding")

    def test_atomic_state_replacement_preserves_requested_owner(self):
        """Ownership applies to the replacement inode, not only an old target."""
        from app.integrations.shared.files import atomic_write_json

        target = self.paths.state_dir / "ownership-probe.json"
        atomic_write_json(target, {"safe": True}, owner=self.owner)
        self.assertEqual(target.stat().st_uid, self.owner[0])
        self.assertEqual(json.loads(target.read_text()), {"safe": True})
        self.assertFalse(any(p.name.startswith(".tmp-") for p in self.paths.state_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
