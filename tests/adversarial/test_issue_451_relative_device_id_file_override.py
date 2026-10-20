"""Issue #451 UAT: a relative DRONE_DEVICE_ID_FILE must never place identity under cwd.

``DRONE_DEVICE_ID_FILE`` is an operator-configured knob that bypasses the userdata-root
check from #286. A relative value would otherwise resolve against the process working
directory, both when persisting a new id and when adopting a planted file. The override
must fail before any read or write, while a valid absolute override keeps working.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity


GETNODE = 0x0242AC110002


class RelativeDeviceIdFileOverrideUatTests(unittest.TestCase):
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

    def test_relative_device_id_file_override_is_rejected_before_any_write(self):
        with self.assertRaises(TypeError):
            device_identity._fake_machine_id(self.userdata)
        self.assertEqual(sorted(path.name for path in self.cwd.iterdir()), [])
        self.assertFalse((self.userdata / "system" / "drone-app" / "device-id").exists())

    def test_planted_cwd_device_id_file_is_never_adopted(self):
        (self.cwd / "device-id").write_text("planted-env-id\n", encoding="utf-8")
        with self.assertRaises(TypeError):
            device_identity._fake_machine_id(self.userdata)
        self.assertEqual((self.cwd / "device-id").read_text(encoding="utf-8"), "planted-env-id\n")

    def test_absolute_device_id_file_override_persists_at_override_path(self):
        override = self.base / "elsewhere" / "device-id"
        override.parent.mkdir()
        with mock.patch.dict(os.environ, {"DRONE_DEVICE_ID_FILE": str(override)}):
            value = device_identity._fake_machine_id(self.userdata)
        self.assertEqual(override.read_text(encoding="utf-8").strip(), value)
        self.assertEqual(sorted(path.name for path in self.cwd.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
