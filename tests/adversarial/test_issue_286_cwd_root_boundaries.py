"""Adversarial boundary tests for Issue #286: identity must never be written under the process cwd.

Expectations come from the issue, not from the implementation. A ``Path('')``, ``Path('.')``
or other relative userdata root resolves against whatever directory the process happened to
start in, so the persisted device-id would land there. The acceptable outcomes for such a
root are a fail-closed ``TypeError`` or a return that writes nothing. A file appearing under
the cwd, or under the cwd's parent (``Path('..')``), is the defect.

Absolute roots are the positive control: they must keep persisting under their own root so
these tests cannot pass by simply refusing everything.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity
from app.common.settings import Settings


GETNODE = 0x0242AC110002
NIC_DERIVED_ID = "02:42:ac:11:00:02"
REJECTED_ROOT_PATHS = (
    Path(""),
    Path("."),
    Path("./"),
    Path("./."),
    Path(".."),
    Path("relative-root"),
    Path("nested/relative/userdata"),
    Path("../sibling"),
)
REJECTED_ROOT_STRINGS = ("", ".", "./", "relative-root", "../sibling")


class CwdRootBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Resolve the base so macOS /var -> /private/var does not hide a leaked path.
        self.base = Path(self._tmp.name).resolve()
        self.cwd = self.base / "cwd"
        self.cwd.mkdir()
        previous = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DRONE_DEVICE_ID_FILE", None)
        getnode = mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE)
        getnode.start()
        self.addCleanup(getnode.stop)

    @staticmethod
    def _tree(directory: Path) -> list:
        return sorted(str(path.relative_to(directory)) for path in directory.rglob("*"))

    def test_rejected_roots_leave_no_trace_under_cwd_or_its_parent(self):
        # Fail-closed (TypeError) is fine. A silent return is fine only if nothing was written.
        # Writing under the parent covers Path('..'), which would otherwise escape the cwd.
        for entry in (device_identity._fake_machine_id, device_identity._machine_id):
            for root in (*REJECTED_ROOT_PATHS, *REJECTED_ROOT_STRINGS):
                with self.subTest(entry=entry.__name__, root=str(root)):
                    try:
                        entry(root)
                    except TypeError:
                        pass
                    self.assertEqual(self._tree(self.cwd), [])
                    self.assertEqual(sorted(path.name for path in self.base.iterdir()), ["cwd"])

    def test_planted_device_id_in_cwd_is_never_adopted(self):
        # The read side of the same leak: a stale or attacker-placed file under the cwd must not
        # be returned as this Drone's identity when the root is relative or empty.
        planted = self.cwd / "system" / "drone-app" / "device-id"
        planted.parent.mkdir(parents=True)
        planted.write_text("planted-cwd-id\n", encoding="utf-8")
        for root in (Path(""), Path(".")):
            with self.subTest(root=str(root)):
                try:
                    value = device_identity._machine_id(root)
                except TypeError:
                    continue
                self.assertNotEqual(value, "planted-cwd-id")
        self.assertEqual(planted.read_text(encoding="utf-8"), "planted-cwd-id\n")

    def test_settings_boot_with_relative_or_empty_root_writes_nothing_under_cwd(self):
        # main() calls Settings.from_env() before anything else, so this is the real boot path.
        for fake in ("0", "1"):
            for root in ("", ".", "relative-root"):
                with self.subTest(use_fake_data=fake, userdata_root=root):
                    env = {
                        "USE_FAKE_DATA": fake,
                        "USERDATA_ROOT": root,
                        "ROMS_ROOT": str(self.base / "roms"),
                        "BIOS_ROOT": str(self.base / "bios"),
                        "SAVES_ROOT": str(self.base / "saves"),
                        "DRONE_STATE_DATABASE_FILE": str(self.base / "state.db"),
                    }
                    with mock.patch.dict(os.environ, env, clear=True):
                        try:
                            Settings.from_env()
                        except TypeError:
                            pass
                    self.assertEqual(self._tree(self.cwd), [])
                    self.assertEqual(sorted(path.name for path in self.base.iterdir()), ["cwd"])

    def test_absolute_root_persists_under_root_and_is_stable(self):
        # Positive control: refusing relative roots must not break the supported absolute path.
        userdata = self.base / "userdata"
        userdata.mkdir()
        for root in (userdata, userdata / "."):
            with self.subTest(root=str(root)):
                first = device_identity._fake_machine_id(Path(root))
                second = device_identity._fake_machine_id(Path(root))
                persisted = (userdata / "system" / "drone-app" / "device-id").read_text(encoding="utf-8").strip()
                self.assertEqual(first, persisted)
                self.assertEqual(first, second)
                self.assertNotEqual(first, NIC_DERIVED_ID)
                self.assertRegex(first, device_identity._DEVICE_ID_PATTERN)
        self.assertEqual(self._tree(self.cwd), [])

    def test_absolute_root_through_settings_persists_under_root_not_cwd(self):
        userdata = self.base / "userdata"
        userdata.mkdir()
        env = {
            "USE_FAKE_DATA": "1",
            "USERDATA_ROOT": str(userdata),
            "ROMS_ROOT": str(userdata / "roms"),
            "BIOS_ROOT": str(userdata / "bios"),
            "SAVES_ROOT": str(userdata / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(userdata / "state.db"),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            device_id = Settings.from_env().device_id
        persisted = (userdata / "system" / "drone-app" / "device-id").read_text(encoding="utf-8").strip()
        self.assertEqual(device_id, persisted)
        self.assertNotEqual(device_id, NIC_DERIVED_ID)
        self.assertEqual(self._tree(self.cwd), [])


if __name__ == "__main__":
    unittest.main()
