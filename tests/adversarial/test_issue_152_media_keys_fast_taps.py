"""Issue #152 boundary: distinct fast taps must not become a reboot hold.

The hotkeygen launcher receives key press/repeat invocations, without a key-up
event. A sequence of discrete taps remains distinct input and must not satisfy
the issue's sustained-hold confirmation requirement.
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


class FastTapHoldBoundaryTests(unittest.TestCase):
    def test_repeated_separate_taps_never_confirm_reboot(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = MediaKeysPaths(Path(tmp) / "integrations" / "media-keys")
            paths.ensure_layout()
            ConfigStore(paths).set_enabled(True)
            control = RecordingControl()
            now = [100.0]
            with mock.patch("app.integrations.media_keys.dispatch._drone_log"):
                results = []
                # Each invocation represents a press followed by a release. Tapping
                # every 450 ms is faster than the current repeat-gap limit, but is
                # still not a sustained hold. Continue well beyond the hold duration.
                for _ in range(12):
                    results.append(handle_press("reboot", paths, control=control,
                                                clock=lambda: now[0])["status"])
                    now[0] += 0.45

            self.assertEqual(control.calls, [],
                             f"separate taps accumulated into reboot confirmation: {results}")


if __name__ == "__main__":
    unittest.main()
