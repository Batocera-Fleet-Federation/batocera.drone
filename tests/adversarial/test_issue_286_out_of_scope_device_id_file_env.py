"""Out-of-scope evidence for Issue #286: a relative DRONE_DEVICE_ID_FILE escapes the root check.

The #286 fix validates the userdata root. ``_device_id_path`` honours ``DRONE_DEVICE_ID_FILE``
verbatim, so a relative override still resolves against the process cwd even when the userdata
root is absolute and valid. This is a different knob from the root argument, so it is recorded
here, not as a blocking #286 assertion.

Each defect is an expectedFailure: the suite passes while the defect exists, and it reports an
unexpected success once the override is validated.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity


GETNODE = 0x0242AC110002


class RelativeDeviceIdFileOverrideTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.cwd = self.base / "cwd"
        self.cwd.mkdir()
        self.userdata = self.base / "userdata"
        self.userdata.mkdir()
        previous = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)
        env = mock.patch.dict(os.environ, {"DRONE_DEVICE_ID_FILE": "device-id"})
        env.start()
        self.addCleanup(env.stop)
        getnode = mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE)
        getnode.start()
        self.addCleanup(getnode.stop)

    @unittest.expectedFailure
    def test_relative_device_id_file_override_writes_nothing_under_cwd(self):
        # Expectation from #286: a valid absolute userdata root must not let a relative override
        # persist identity under the process cwd. Currently it writes ./device-id.
        device_identity._fake_machine_id(self.userdata)
        self.assertEqual(sorted(path.name for path in self.cwd.iterdir()), [])

    @unittest.expectedFailure
    def test_relative_device_id_file_override_never_adopts_planted_cwd_file(self):
        # Read side of the same override: a file planted in the cwd is currently returned as this
        # Drone's identity instead of a freshly persisted one under the absolute root.
        (self.cwd / "device-id").write_text("planted-env-id\n", encoding="utf-8")
        self.assertNotEqual(device_identity._fake_machine_id(self.userdata), "planted-env-id")


if __name__ == "__main__":
    unittest.main()
