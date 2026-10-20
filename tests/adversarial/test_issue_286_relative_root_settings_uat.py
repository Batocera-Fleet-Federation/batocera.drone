"""Issue #286 real-process UAT: a relative or empty USERDATA_ROOT must not write under the cwd.

Expected, from the issue (not from any patch):

* Each check runs ``Settings.from_env()`` in a fresh interpreter whose process cwd is an empty
  directory, with ``USERDATA_ROOT`` set to ``''``, ``.`` or a relative name. The Drone must either
  refuse the root (TypeError) or mint an id without leaving any file or directory under the cwd.
* A ``system/drone-app/device-id`` planted in that cwd must not be adopted.
* ``uuid.getnode()`` is patched to 0x0242AC110002 in the child; that host-derived value must never
  be returned in fake-data mode.
* The absolute-root control case still persists its id under the root, not under the cwd.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TIMEOUT_SECONDS = 120
HOST_DERIVED = "02:42:ac:11:00:02"
PLANTED = "aa:bb:cc:dd:ee:01"

CHILD = r"""
from unittest import mock
from app.common import device_identity
from app.common.settings import Settings
with mock.patch.object(device_identity.uuid, "getnode", return_value=0x0242AC110002):
    try:
        print("ID=" + Settings.from_env().device_id)
    except TypeError as error:
        print("REJECTED=" + str(error))
"""


def _tree(root: Path) -> list:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


class RelativeRootSettingsUatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _fresh_cwd(self, name: str) -> Path:
        cwd = self.root / name
        cwd.mkdir()
        return cwd

    def _run_child(self, cwd: Path, userdata_value: str, fake: str):
        side = self.root / "side"
        side.mkdir(exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(REPO),
            "PYTHONDONTWRITEBYTECODE": "1",
            "USE_FAKE_DATA": fake,
            "USERDATA_ROOT": userdata_value,
            "ROMS_ROOT": str(side / "roms"),
            "BIOS_ROOT": str(side / "bios"),
            "SAVES_ROOT": str(side / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(side / "state.db"),
        }
        return subprocess.run(
            [sys.executable, "-c", CHILD],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )

    def _child_id(self, result) -> "str | None":
        """Return the id the child printed, or None when it rejected the root."""
        self.assertEqual(result.returncode, 0, f"child crashed: {result.stderr}")
        for line in result.stdout.splitlines():
            if line.startswith("ID="):
                return line[len("ID="):]
            if line.startswith("REJECTED="):
                return None
        self.fail(f"child neither returned an id nor rejected the root: {result.stdout!r} {result.stderr!r}")

    def test_relative_and_empty_userdata_root_leaves_cwd_untouched(self):
        for fake in ("1", "0"):
            for userdata_value in ("", ".", "relative-userdata"):
                with self.subTest(fake=fake, userdata_root=userdata_value):
                    cwd = self._fresh_cwd(f"cwd-{fake}-{len(userdata_value)}-{userdata_value or 'empty'}")
                    before = _tree(cwd)
                    value = self._child_id(self._run_child(cwd, userdata_value, fake))
                    self.assertEqual(_tree(cwd), before, "Settings wrote under the process cwd")
                    if fake == "1":
                        self.assertNotEqual(value, HOST_DERIVED)

    def test_planted_cwd_device_id_is_not_adopted_by_settings(self):
        cwd = self._fresh_cwd("planted-cwd")
        planted_path = cwd / "system" / "drone-app" / "device-id"
        planted_path.parent.mkdir(parents=True)
        planted_path.write_text(PLANTED + "\n", encoding="utf-8")
        value = self._child_id(self._run_child(cwd, ".", "1"))
        self.assertNotEqual(value, PLANTED, "Settings adopted a device-id planted in the cwd")
        self.assertEqual(planted_path.read_text(encoding="utf-8"), PLANTED + "\n")

    def test_absolute_userdata_root_control_persists_under_root_only(self):
        cwd = self._fresh_cwd("absolute-control-cwd")
        userdata = self.root / "userdata"
        (userdata / "system" / "drone-app").mkdir(parents=True)
        value = self._child_id(self._run_child(cwd, str(userdata), "1"))
        self.assertIsNotNone(value)
        self.assertNotEqual(value, HOST_DERIVED)
        persisted = (userdata / "system" / "drone-app" / "device-id").read_text(encoding="utf-8").strip()
        self.assertEqual(persisted, value)
        self.assertEqual(_tree(cwd), [])


if __name__ == "__main__":
    unittest.main()
