"""Out-of-scope evidence for #227: Path('') / Path('.') persist under cwd.

Issue #227 is the unscoped helper default: ``_fake_machine_id()`` and
``_fake_machine_id(None)`` used to return ``uuid.getnode()``. A related leftover
is a caller that *does* pass a Path, but that Path names the current directory.

Evidence: ``_require_real_path`` accepts ``Path('')`` and ``Path('.')`` because
they are ``os.PathLike``. ``_machine_id`` then treats them as a real userdata
root, creates ``./system/drone-app/``, and writes ``device-id`` there. The
returned value is a fresh logical id, not getnode, so this is not the #227
regression. It is a cwd-write blast radius for a relative/empty Path.

Why outside scope: the issue names the Optional/None default. Production
``Settings.from_env()`` always passes the ``USERDATA_ROOT`` Path. These
assertions pin the current write so the finding has an executable reproduction.
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

    def test_empty_and_dot_paths_write_device_id_under_cwd(self) -> None:
        target = self.cwd / "system" / "drone-app" / "device-id"
        with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE):
            empty_value = _fake_machine_id(Path(""))
        self.assertTrue(target.is_file(), "Path('') persisted a device-id under the process cwd")
        self.assertEqual(target.read_text(encoding="utf-8").strip(), empty_value)
        self.assertNotEqual(empty_value, RUNTIME_MAC)
        target.write_text("keep-227\n", encoding="utf-8")
        with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE):
            dot_value = _fake_machine_id(Path("."))
        self.assertEqual(dot_value, "keep-227")
        self.assertEqual(target.read_text(encoding="utf-8").strip(), "keep-227")


if __name__ == "__main__":
    unittest.main()
