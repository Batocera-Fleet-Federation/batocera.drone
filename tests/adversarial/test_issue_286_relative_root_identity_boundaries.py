"""Issue #286 boundary probes: relative and empty identity roots must never touch the process cwd.

Expected, from the issue and the identity invariants (not from any implementation):

* ``Path('')``, ``Path('.')`` and other cwd-relative roots resolve against the process cwd. No
  device-id file, directory or other entry may appear under the cwd for them, in fake or real mode.
* Rejecting a relative root with ``TypeError`` is an acceptable outcome. Returning an id is
  acceptable only when the cwd is left untouched.
* A ``system/drone-app/device-id`` planted in the cwd is never adopted as this Drone's identity.
* An absolute ``USERDATA_ROOT`` keeps working: the id persists under that root, is stable across
  calls, and the cwd stays untouched.
* ``uuid.getnode()`` is never returned as the fake-mode id (#227).
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.common import device_identity  # noqa: E402

HOST_DERIVED = "02:42:ac:11:00:02"
PLANTED = "aa:bb:cc:dd:ee:01"
MAC_SHAPED = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


@contextmanager
def _cwd(path: Path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _tree(root: Path) -> list:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _call(fn, root):
    """Return the helper's id, or None when it rejects the root with TypeError."""
    try:
        return fn(root)
    except TypeError:
        return None


class RelativeRootIdentityBoundaryTests(unittest.TestCase):
    RELATIVE_ROOTS = (
        Path(""),
        Path("."),
        Path("./"),
        Path("relative-root"),
        Path("nested/relative"),
        "",
        ".",
        "relative-root",
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._getnode = mock.patch.object(device_identity.uuid, "getnode", return_value=0x0242AC110002)
        self._getnode.start()

    def tearDown(self):
        self._getnode.stop()
        self._tmp.cleanup()

    def _fresh_cwd(self, name: str) -> Path:
        cwd = self.root / name
        cwd.mkdir()
        return cwd

    def test_fake_machine_id_never_writes_under_cwd_for_relative_roots(self):
        for index, root in enumerate(self.RELATIVE_ROOTS):
            with self.subTest(root=repr(root)):
                cwd = self._fresh_cwd(f"fake-{index}")
                with _cwd(cwd):
                    value = _call(device_identity._fake_machine_id, root)
                self.assertEqual(_tree(cwd), [], "relative root persisted under the process cwd")
                self.assertNotEqual(value, HOST_DERIVED)

    def test_machine_id_never_writes_under_cwd_for_relative_roots(self):
        # The real-mode helper shares the same path resolution, so it has the same boundary.
        for index, root in enumerate(self.RELATIVE_ROOTS):
            with self.subTest(root=repr(root)):
                cwd = self._fresh_cwd(f"real-{index}")
                with _cwd(cwd):
                    _call(device_identity._machine_id, root)
                self.assertEqual(_tree(cwd), [], "relative root persisted under the process cwd")

    def test_planted_cwd_device_id_is_never_adopted(self):
        index = 0
        for helper in (device_identity._fake_machine_id, device_identity._machine_id):
            for root in (Path("."), Path("")):
                index += 1
                with self.subTest(helper=helper.__name__, root=repr(root)):
                    cwd = self._fresh_cwd(f"planted-{index}")
                    planted_path = cwd / "system" / "drone-app" / "device-id"
                    planted_path.parent.mkdir(parents=True)
                    planted_path.write_text(PLANTED + "\n", encoding="utf-8")
                    with _cwd(cwd):
                        value = _call(helper, root)
                    self.assertNotEqual(value, PLANTED, "cwd-planted device-id was adopted")
                    self.assertEqual(planted_path.read_text(encoding="utf-8"), PLANTED + "\n")

    def test_absolute_root_still_persists_stably_and_leaves_cwd_empty(self):
        cwd = self._fresh_cwd("abs-cwd")
        userdata = self.root / "userdata"
        with _cwd(cwd):
            first = device_identity._fake_machine_id(userdata)
            second = device_identity._fake_machine_id(userdata)
        self.assertRegex(first, MAC_SHAPED)
        self.assertNotEqual(first, HOST_DERIVED)
        self.assertEqual(first, second)
        persisted = (userdata / "system" / "drone-app" / "device-id").read_text(encoding="utf-8").strip()
        self.assertEqual(persisted, first)
        self.assertEqual(_tree(cwd), [])

    def test_absolute_root_with_missing_parents_is_created_under_root_not_cwd(self):
        cwd = self._fresh_cwd("missing-parents-cwd")
        userdata = self.root / "fresh" / "deep" / "userdata"
        with _cwd(cwd):
            value = device_identity._fake_machine_id(userdata)
        self.assertRegex(value, MAC_SHAPED)
        self.assertEqual(
            (userdata / "system" / "drone-app" / "device-id").read_text(encoding="utf-8").strip(),
            value,
        )
        self.assertEqual(_tree(cwd), [])


if __name__ == "__main__":
    unittest.main()
