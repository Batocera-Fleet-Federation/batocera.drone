"""Issue #286 boundary probes: the device-id persistence helpers must never touch the process cwd.

Expected, from the issue and the identity invariant (not from the current implementation):

* A device-id is never persisted under, read from, or adopted out of the process cwd when the
  userdata root is cwd-relative (``Path('')``, ``Path('.')``, ``Path('relative-root')``).
* The fix for #286 guards ``_machine_id``/``_fake_machine_id``, but the persistence helpers
  (``_device_id_path``, ``_write_persisted_machine_id``, ``_read_persisted_machine_id``) resolve
  the root themselves. The same boundary must hold for them, because the fix's own comment says
  the persisted id "always lives under the configured userdata tree".
* Rejecting a relative root with ``TypeError`` is an acceptable outcome. Any other outcome must
  leave the cwd untouched.
* An absolute root keeps working through the same helpers (no over-correction).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.common import device_identity  # noqa: E402

PLANTED = "aa:bb:cc:dd:ee:01"
RELATIVE_ROOTS = (Path(""), Path("."), Path("relative-root"))


def _tree(root: Path) -> list:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


class DeviceIdHelperCwdBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.cwd = self.base / "cwd"
        (self.cwd / "system" / "drone-app").mkdir(parents=True)
        self._previous = os.getcwd()
        os.chdir(self.cwd)
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        # An override path would bypass the root entirely; these tests exercise the root.
        os.environ.pop("DRONE_DEVICE_ID_FILE", None)

    def tearDown(self):
        self._env.stop()
        os.chdir(self._previous)
        self._tmp.cleanup()

    def _plant_cwd_device_id(self):
        (self.cwd / "system" / "drone-app" / "device-id").write_text(f"{PLANTED}\n", encoding="utf-8")

    def test_device_id_path_never_resolves_relative_to_cwd(self):
        for root in RELATIVE_ROOTS:
            with self.subTest(root=repr(root)):
                try:
                    path = device_identity._device_id_path(root)
                except TypeError:
                    continue
                self.assertTrue(path.is_absolute(), f"device-id path {path} is cwd-relative")

    def test_write_helper_never_writes_under_cwd_for_relative_roots(self):
        before = _tree(self.cwd)
        for root in RELATIVE_ROOTS:
            with self.subTest(root=repr(root)):
                try:
                    device_identity._write_persisted_machine_id(root, PLANTED)
                except TypeError:
                    pass
                self.assertEqual(_tree(self.cwd), before, "device-id written under the process cwd")
                self.assertFalse((self.cwd / "system" / "drone-app" / "device-id").exists())

    def test_read_helper_never_adopts_planted_cwd_device_id_for_relative_roots(self):
        self._plant_cwd_device_id()
        for root in RELATIVE_ROOTS:
            with self.subTest(root=repr(root)):
                try:
                    value = device_identity._read_persisted_machine_id(root)
                except TypeError:
                    continue
                self.assertNotEqual(value, PLANTED, "planted cwd device-id adopted as identity")

    def test_absolute_root_still_round_trips_through_helpers(self):
        root = self.base / "userdata"
        (root / "system" / "drone-app").mkdir(parents=True)
        self.assertTrue(device_identity._write_persisted_machine_id(root, PLANTED))
        self.assertEqual(device_identity._read_persisted_machine_id(root), PLANTED)
        self.assertEqual(
            device_identity._device_id_path(root),
            root / "system" / "drone-app" / "device-id",
        )
        self.assertEqual(_tree(self.cwd), ["system", "system/drone-app"])


if __name__ == "__main__":
    unittest.main()
