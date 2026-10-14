"""Fake-data (``USE_FAKE_DATA=1``) identity must persist under ``USERDATA_ROOT``.

Regression for #199: ``_fake_machine_id`` used to ignore the userdata root and fall back to
``uuid.getnode()``, so two fake-data processes on one host shared a host-derived device id.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity
from app.common.settings import Settings


class FakeDataIdentityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _env(self, userdata: Path) -> dict:
        return {
            "USE_FAKE_DATA": "1",
            "USERDATA_ROOT": str(userdata),
            "ROMS_ROOT": str(userdata / "roms"),
            "BIOS_ROOT": str(userdata / "bios"),
            "SAVES_ROOT": str(userdata / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(userdata / "state.db"),
            "DRONE_DEVICE_ID_FILE": str(userdata / "system" / "drone-app" / "device-id"),
        }

    def test_fake_device_id_is_persisted_and_stable(self):
        userdata = self.root / "a"
        (userdata / "system" / "drone-app").mkdir(parents=True)
        with mock.patch.dict("os.environ", self._env(userdata), clear=True):
            first = Settings.from_env().device_id
            second = Settings.from_env().device_id
        self.assertEqual(first, second)
        persisted = (userdata / "system" / "drone-app" / "device-id").read_text(encoding="utf-8").strip()
        self.assertEqual(persisted, first)

    def test_two_fake_roots_do_not_share_host_derived_id(self):
        ids = []
        with mock.patch.object(device_identity.uuid, "getnode", return_value=0x0242AC110002):
            for name in ("one", "two"):
                userdata = self.root / name
                (userdata / "system" / "drone-app").mkdir(parents=True)
                with mock.patch.dict("os.environ", self._env(userdata), clear=True):
                    ids.append(Settings.from_env().device_id)
        self.assertNotEqual(ids[0], ids[1])
        self.assertNotIn("02:42:ac:11:00:02", ids)

    def test_fake_machine_id_without_root_never_returns_host_derived_id(self):
        # Regression for #227: an unscoped call used to fall through to uuid.getnode().
        with mock.patch.object(device_identity.uuid, "getnode", return_value=0x0242AC110002):
            with self.assertRaises(TypeError):
                device_identity._fake_machine_id()
            with self.assertRaises(TypeError):
                device_identity._fake_machine_id(None)
        self.assertFalse((self.root / "system").exists())

    def test_fake_machine_id_with_root_persists_under_userdata(self):
        userdata = self.root / "scoped"
        (userdata / "system" / "drone-app").mkdir(parents=True)
        with mock.patch.object(device_identity.uuid, "getnode", return_value=0x0242AC110002):
            value = device_identity._fake_machine_id(userdata)
        self.assertNotEqual(value, "02:42:ac:11:00:02")
        persisted = (userdata / "system" / "drone-app" / "device-id").read_text(encoding="utf-8").strip()
        self.assertEqual(persisted, value)


if __name__ == "__main__":
    unittest.main()
