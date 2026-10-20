"""Regression tests for #458: device-id persistence must never follow a symlink."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity
from app.common.settings import Settings


@unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
class DeviceIdSymlinkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.userdata = self.root / "ud"
        self.drone_dir = self.userdata / "system" / "drone-app"
        self.victim = self.root / "victim" / "target.conf"
        self.victim.parent.mkdir()
        self.victim.write_text("root-owned-config=1", encoding="utf-8")

    def _env(self, **extra):
        env = {
            "USERDATA_ROOT": str(self.userdata),
            "ROMS_ROOT": str(self.userdata / "roms"),
            "BIOS_ROOT": str(self.userdata / "bios"),
            "SAVES_ROOT": str(self.userdata / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.userdata / "state.db"),
        }
        env.update(extra)
        return env

    def _assert_victim_untouched(self):
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "root-owned-config=1")

    def test_symlinked_id_file_target_is_not_overwritten(self):
        self.drone_dir.mkdir(parents=True)
        link = self.drone_dir / "device-id"
        link.symlink_to(self.victim)
        with mock.patch.object(device_identity.uuid, "getnode", return_value=0x0242AC110002):
            value = device_identity._machine_id(self.userdata)
        self._assert_victim_untouched()
        self.assertTrue(link.is_symlink())
        # Unpersistable: falls back to a host-derived id rather than writing through the link.
        self.assertTrue(device_identity._normalize_device_id(value))

    def test_dangling_symlink_is_not_created_through(self):
        self.drone_dir.mkdir(parents=True)
        missing = self.root / "victim" / "new-file"
        (self.drone_dir / "device-id").symlink_to(missing)
        self.assertFalse(device_identity._write_persisted_machine_id(self.userdata, "aa:bb:cc:dd:ee:ff"))
        self.assertFalse(missing.exists())

    def test_symlinked_parent_directory_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.userdata / "system").mkdir(parents=True)
        (self.userdata / "system" / "drone-app").symlink_to(outside, target_is_directory=True)
        self.assertFalse(device_identity._write_persisted_machine_id(self.userdata, "aa:bb:cc:dd:ee:ff"))
        self.assertEqual(list(outside.iterdir()), [])

    def test_mkdir_does_not_create_directories_through_a_symlinked_parent(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.userdata.mkdir()
        (self.userdata / "system").symlink_to(outside, target_is_directory=True)
        self.assertFalse(device_identity._ensure_device_id_dir(self.userdata))
        self.assertEqual(list(outside.iterdir()), [])
        with mock.patch.object(device_identity.uuid, "getnode", return_value=0x0242AC110002):
            device_identity._machine_id(self.userdata)
        self.assertEqual(list(outside.iterdir()), [])

    def test_settings_from_env_does_not_write_through_symlink(self):
        self.drone_dir.mkdir(parents=True)
        (self.drone_dir / "device-id").symlink_to(self.victim)
        with mock.patch.dict("os.environ", self._env(), clear=True):
            settings = Settings.from_env()
        self.assertTrue(device_identity._normalize_device_id(settings.device_id))
        self._assert_victim_untouched()

    def test_explicit_id_file_override_symlink_is_refused(self):
        self.drone_dir.mkdir(parents=True)
        link = self.drone_dir / "custom-id"
        link.symlink_to(self.victim)
        with mock.patch.dict("os.environ", {"DRONE_DEVICE_ID_FILE": str(link)}):
            self.assertFalse(device_identity._write_persisted_machine_id(self.userdata, "aa:bb:cc:dd:ee:ff"))
        self._assert_victim_untouched()

    def test_symlinked_userdata_root_itself_is_still_allowed(self):
        real = self.root / "real-drive"
        real.mkdir()
        self.userdata.symlink_to(real, target_is_directory=True)
        self.assertTrue(device_identity._ensure_device_id_dir(self.userdata))
        generated = device_identity._machine_id(self.userdata)
        self.assertEqual(device_identity._read_persisted_machine_id(self.userdata), generated)

    def test_normal_write_is_atomic_and_leaves_no_temp_files(self):
        self.drone_dir.mkdir(parents=True)
        generated = device_identity._machine_id(self.userdata)
        self.assertEqual((self.drone_dir / "device-id").read_text(encoding="utf-8"), f"{generated}\n")
        self.assertEqual([p.name for p in self.drone_dir.iterdir()], ["device-id"])
        self.assertTrue(device_identity._write_persisted_machine_id(self.userdata, "aa:bb:cc:dd:ee:ff"))
        self.assertEqual(device_identity._read_persisted_machine_id(self.userdata), "aa:bb:cc:dd:ee:ff")
        self.assertEqual([p.name for p in self.drone_dir.iterdir()], ["device-id"])

    def test_missing_parent_still_not_created_by_write(self):
        self.userdata.mkdir()
        self.assertFalse(device_identity._write_persisted_machine_id(self.userdata, "aa:bb:cc:dd:ee:ff"))


if __name__ == "__main__":
    unittest.main()
