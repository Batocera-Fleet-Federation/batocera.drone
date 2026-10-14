"""Issue #227 real-process UAT: unscoped ``_fake_machine_id`` against the real host.

Expected, from the issue and the fake-data identity invariant (not from the patch):

* ``uuid.getnode()`` is NOT mocked. Every check runs in a fresh interpreter, so the
  host's real getnode is the value the unscoped helper returned before #227.
* An unscoped call (no argument, positional ``None``, keyword ``None``) must raise
  ``TypeError`` in that process and never print a host-derived id.
* The unscoped call must not write anything under the process cwd, under a
  ``USERDATA_ROOT`` or ``DRONE_DEVICE_ID_FILE`` set in the environment, or under ``HOME``.
* A real ``Settings.from_env()`` boot with ``USE_FAKE_DATA=1`` and a real
  ``USERDATA_ROOT`` must mint a logical id that is not the host getnode, persist it
  under that root, and return the same id on the next process start.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TIMEOUT_SECONDS = 60
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"

UNSCOPED_CHILD = r"""
import sys
sys.path.insert(0, sys.argv[1])
from app.common.device_identity import _fake_machine_id
for label, call in (
    ("omitted", lambda: _fake_machine_id()),
    ("positional None", lambda: _fake_machine_id(None)),
    ("keyword None", lambda: _fake_machine_id(userdata_root=None)),
):
    try:
        value = call()
    except TypeError as exc:
        print(f"{label}: TypeError {exc}")
    else:
        print(f"{label}: RETURNED {value}")
"""

SETTINGS_CHILD = r"""
import sys
sys.path.insert(0, sys.argv[1])
from app.common.settings import Settings
print(Settings.from_env().device_id)
"""


def host_getnode_id() -> str:
    node = uuid.getnode()
    return ":".join(f"{(node >> shift) & 0xFF:02x}" for shift in range(40, -1, -8))


class FakeMachineIdRealProcessUatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="issue-227-proc-")
        self.addCleanup(self._tmpdir.cleanup)
        self.work = Path(self._tmpdir.name).resolve()
        self.home = self.work / "home"
        self.userdata = self.work / "userdata"
        self.cwd = self.work / "cwd"
        for path in (self.home, self.cwd):
            path.mkdir()

    def _env(self, **extra: str) -> dict:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "USERDATA_ROOT": str(self.userdata),
            "ROMS_ROOT": str(self.userdata / "roms"),
            "BIOS_ROOT": str(self.userdata / "bios"),
            "SAVES_ROOT": str(self.userdata / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.userdata / "state.db"),
        }
        env.update(extra)
        return env

    def _run(self, code: str, env: dict) -> subprocess.CompletedProcess:
        # -I drops the script directory and PYTHON* variables from sys.path, so the only
        # import root is the explicit repo path passed as argv[1].
        return subprocess.run(
            [sys.executable, "-I", "-c", code, str(REPO)],
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )

    def test_unscoped_calls_fail_closed_in_a_fresh_interpreter(self) -> None:
        custom_id_file = self.userdata / "custom-device-id"
        env = self._env(DRONE_DEVICE_ID_FILE=str(custom_id_file))
        completed = self._run(UNSCOPED_CHILD, env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.strip().splitlines()
        self.assertEqual(len(lines), 3, completed.stdout)
        for line in lines:
            with self.subTest(line=line):
                self.assertIn(": TypeError ", line)
                self.assertNotIn("RETURNED", line)
        self.assertNotIn(host_getnode_id(), completed.stdout)
        self.assertEqual(list(self.cwd.iterdir()), [])
        self.assertEqual(list(self.home.rglob("*")), [])
        self.assertEqual(list(self.userdata.rglob("*")), [])
        self.assertFalse(custom_id_file.exists())

    def test_real_settings_boot_mints_a_logical_id_and_keeps_it_across_starts(self) -> None:
        env = self._env(USE_FAKE_DATA="1")
        first_run = self._run(SETTINGS_CHILD, env)
        self.assertEqual(first_run.returncode, 0, first_run.stderr)
        first = first_run.stdout.strip()
        second_run = self._run(SETTINGS_CHILD, env)
        self.assertEqual(second_run.returncode, 0, second_run.stderr)
        second = second_run.stdout.strip()

        self.assertRegex(first, MAC_SHAPED)
        self.assertEqual(int(first.split(":")[0], 16) & 0x03, 0x02)
        self.assertNotEqual(first, host_getnode_id())
        self.assertEqual(first, second)
        persisted = self.userdata / "system" / "drone-app" / "device-id"
        self.assertEqual(persisted.read_text(encoding="utf-8").strip(), first)


if __name__ == "__main__":
    unittest.main()
