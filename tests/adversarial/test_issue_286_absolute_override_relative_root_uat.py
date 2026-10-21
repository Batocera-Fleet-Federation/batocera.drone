"""Issue #286 regression check: the cwd boundary must not disable an absolute DRONE_DEVICE_ID_FILE.

Expected, from the issue: a relative or empty userdata root must never persist a device-id under
the process cwd. The defect is the cwd write, not the root argument itself. When the operator
pins an *absolute* ``DRONE_DEVICE_ID_FILE``, the id lands at that absolute path, so no cwd write
can happen and the Drone should keep booting with the id it persisted there.

Each check below therefore expects:

* the id is minted and stored at the absolute override,
* nothing appears under the cwd,
* the host-derived ``uuid.getnode()`` value is never returned,
* a second start reads the same id back.

At the Settings boot level the #454 contract applies first: a relative ``USERDATA_ROOT`` is refused
by name (ValueError) before any identity is read, even when an absolute override is pinned. A blank
root is unset, so the absolute override keeps the id. The settings-boot check below pins both halves.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity

REPO = Path(__file__).resolve().parents[2]
TIMEOUT_SECONDS = 120
GETNODE = 0x0242AC110002
HOST_DERIVED = "02:42:ac:11:00:02"

CHILD = r"""
from unittest import mock
from app.common import device_identity
from app.common.settings import Settings
with mock.patch.object(device_identity.uuid, "getnode", return_value=0x0242AC110002):
    try:
        print("ID=" + Settings.from_env().device_id)
    except ValueError as error:
        print("REJECTED=" + str(error))
"""


class AbsoluteOverrideRelativeRootTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.cwd = self.base / "cwd"
        self.cwd.mkdir()
        self.override = self.base / "pinned" / "device-id"
        self.previous_cwd = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, self.previous_cwd)
        env = mock.patch.dict(os.environ, {"DRONE_DEVICE_ID_FILE": str(self.override)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        getnode = mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE)
        getnode.start()
        self.addCleanup(getnode.stop)

    def _cwd_tree(self):
        return sorted(str(path.relative_to(self.cwd)) for path in self.cwd.rglob("*"))

    def test_fake_machine_id_with_relative_root_persists_only_to_absolute_override(self):
        for relative in (Path(""), Path("."), Path("rel-root")):
            with self.subTest(root=str(relative)):
                if self.override.exists():
                    self.override.unlink()
                first = device_identity._fake_machine_id(relative)
                self.assertNotEqual(first, HOST_DERIVED)
                self.assertEqual(self.override.read_text(encoding="utf-8").strip(), first)
                self.assertEqual(device_identity._fake_machine_id(relative), first)
                self.assertEqual(self._cwd_tree(), [], "relative root wrote under the cwd")

    def test_machine_id_with_relative_root_persists_only_to_absolute_override(self):
        first = device_identity._machine_id(Path(""))
        self.assertNotEqual(first, HOST_DERIVED)
        self.assertEqual(self.override.read_text(encoding="utf-8").strip(), first)
        self.assertEqual(device_identity._machine_id(Path("")), first)
        self.assertEqual(self._cwd_tree(), [], "relative root wrote under the cwd")

    def test_settings_boot_with_absolute_override_refuses_relative_root_and_keeps_blank_identity(self):
        side = self.base / "side"
        side.mkdir()
        for userdata_value in ("", ".", "rel-root"):
            with self.subTest(USERDATA_ROOT=userdata_value):
                if self.override.exists():
                    self.override.unlink()
                env = {
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONPATH": str(REPO),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "USE_FAKE_DATA": "1",
                    "USERDATA_ROOT": userdata_value,
                    "DRONE_DEVICE_ID_FILE": str(self.override),
                    "ROMS_ROOT": str(side / "roms"),
                    "BIOS_ROOT": str(side / "bios"),
                    "SAVES_ROOT": str(side / "saves"),
                    "DRONE_STATE_DATABASE_FILE": str(side / "state.db"),
                }
                result = subprocess.run(
                    [sys.executable, "-c", CHILD],
                    cwd=str(self.cwd),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_SECONDS,
                )
                self.assertEqual(result.returncode, 0, f"child crashed: {result.stderr}")
                lines = [line for line in result.stdout.splitlines() if line.startswith(("ID=", "REJECTED="))]
                self.assertTrue(lines, f"child printed neither an id nor a rejection: {result.stdout!r}")
                if userdata_value:
                    self.assertTrue(lines[-1].startswith("REJECTED="), f"relative root was not refused: {lines[-1]}")
                    self.assertFalse(self.override.exists(), "a refused boot must not mint an id")
                    self.assertEqual(self._cwd_tree(), [], "relative USERDATA_ROOT wrote under the cwd")
                    continue
                self.assertTrue(lines[-1].startswith("ID="), f"absolute override boot was rejected: {lines[-1]}")
                booted = lines[-1][len("ID="):]
                self.assertNotEqual(booted, HOST_DERIVED)
                self.assertEqual(self.override.read_text(encoding="utf-8").strip(), booted)
                self.assertEqual(self._cwd_tree(), [], "relative USERDATA_ROOT wrote under the cwd")


if __name__ == "__main__":
    unittest.main()
