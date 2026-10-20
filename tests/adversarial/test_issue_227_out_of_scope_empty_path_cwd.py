"""Adjudication for #286: cwd-relative roots must be rejected without touching cwd.

This test previously pinned the failing behavior as an out-of-scope #227 finding.
Issue #286 specifically asks to remove that behavior, so the assertion is inverted:
both empty and dot paths must be rejected and leave the cwd unchanged.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity
from app.common.device_identity import _fake_machine_id


GETNODE = 0x0242AC110002
RUNTIME_MAC = "02:42:ac:11:00:02"


class EmptyPathCwdEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="issue-227-cwd-")
        self.cwd = Path(self._tmpdir.name).resolve()
        os.chdir(self.cwd)
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._env.stop()
        self._tmpdir.cleanup()

    def test_empty_and_dot_paths_are_rejected_without_writing_under_cwd(self) -> None:
        target = self.cwd / "system" / "drone-app" / "device-id"
        with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE):
            for root in (Path(""), Path("."), Path("relative-root")):
                with self.subTest(root=repr(root)):
                    with self.assertRaises(TypeError):
                        _fake_machine_id(root)
        self.assertFalse(target.exists(), "cwd-relative root persisted a device-id under cwd")
        self.assertEqual(sorted(path.name for path in self.cwd.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
