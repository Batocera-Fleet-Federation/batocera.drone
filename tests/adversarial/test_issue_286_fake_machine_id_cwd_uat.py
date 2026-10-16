"""Issue #286 UAT: fake-data identity storage must never be rooted at cwd.

The fake identity helper persists its generated logical device id below the
explicit userdata root.  ``Path(\"\")`` and ``Path(\".\")`` both name the
process working directory, not an installation-owned userdata tree.  They must
therefore fail before identity generation or filesystem writes.  A real absolute
root remains valid and is the only place the persistence file may be created.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity
from app.common.device_identity import _fake_machine_id


class FakeMachineIdCwdUatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="issue-286-uat-")
        self.work = Path(self._tmpdir.name).resolve()
        self.cwd = self.work / "process-cwd"
        self.userdata = self.work / "userdata"
        self.cwd.mkdir()
        os.chdir(self.cwd)
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._env.stop()
        self._tmpdir.cleanup()

    def test_empty_and_dot_paths_fail_before_identity_or_cwd_write(self) -> None:
        """Neither spelling may create ``./system/drone-app/device-id``."""
        cwd_id_path = self.cwd / "system" / "drone-app" / "device-id"
        with mock.patch.object(device_identity, "_machine_id") as machine_id, mock.patch.object(
            device_identity.uuid, "getnode"
        ) as getnode:
            for root in (Path(""), Path(".")):
                with self.subTest(root=repr(root)):
                    with self.assertRaisesRegex(TypeError, "absolute filesystem path"):
                        _fake_machine_id(root)
        machine_id.assert_not_called()
        getnode.assert_not_called()
        self.assertFalse(cwd_id_path.exists())
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_absolute_root_persists_only_beneath_that_root(self) -> None:
        """Rejecting cwd aliases must not reject a configured absolute userdata root."""
        expected = "02:00:00:00:02:86"
        with mock.patch.object(device_identity, "random_device_id", return_value=expected):
            value = _fake_machine_id(self.userdata)
        persisted = self.userdata / "system" / "drone-app" / "device-id"
        self.assertEqual(value, expected)
        self.assertEqual(persisted.read_text(encoding="utf-8").strip(), expected)
        self.assertEqual(list(self.cwd.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
