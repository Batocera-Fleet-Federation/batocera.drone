"""Issue #287 adversarial UAT: str and PathLike userdata roots through fake identity.

Expected behavior is derived from the issue, not from the implementation: ``_require_real_path``
accepts ``str`` and ``os.PathLike``, so every accepted type must either (a) persist identity under
its own absolute root without touching ``uuid.getnode()``, or (b) fail closed with a ``TypeError``
before any identity is generated or any file is written. A raw join failure is never acceptable.
"""

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity
from app.common.settings import Settings

MAC_SHAPE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
HOST_NIC = 0x0242AC110002
HOST_NIC_TEXT = "02:42:ac:11:00:02"


class _PathLike:
    """Minimal os.PathLike that is not a pathlib.Path subclass."""

    def __init__(self, value):
        self._value = value

    def __fspath__(self):
        return self._value


def _snapshot(root: Path):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


class StrPathLikeIdentityUatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-287-uat-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.cwd = self.base / "cwd"
        self.cwd.mkdir()
        self._previous_cwd = Path.cwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, self._previous_cwd)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DRONE_DEVICE_ID_FILE", None)
        os.environ.pop("DRONE_DEVICE_ID", None)

    def _root(self, name: str) -> Path:
        root = self.base / name
        (root / "system" / "drone-app").mkdir(parents=True)
        return root

    def test_absolute_str_and_pathlike_roots_persist_under_their_own_root(self):
        cases = {
            "str": lambda path: str(path),
            "trailing-slash-dot": lambda path: f"{path}/./",
            "custom-pathlike": lambda path: _PathLike(str(path)),
        }
        with mock.patch.object(device_identity.uuid, "getnode", return_value=HOST_NIC) as getnode:
            for label, supply in cases.items():
                with self.subTest(root_type=label):
                    root = self._root(f"root-{label}")
                    value = device_identity._fake_machine_id(supply(root))
                    self.assertRegex(value, MAC_SHAPE)
                    self.assertNotEqual(value, HOST_NIC_TEXT)
                    persisted = root / "system" / "drone-app" / "device-id"
                    self.assertEqual(persisted.read_text(encoding="utf-8").strip(), value)
                    self.assertEqual(device_identity._fake_machine_id(supply(root)), value)
        getnode.assert_not_called()
        self.assertFalse((self.base / "system").exists())

    def test_str_and_path_views_of_one_root_share_the_same_persisted_identity(self):
        root = self._root("shared")
        persisted = root / "system" / "drone-app" / "device-id"
        persisted.write_text("adversarial-287-id\n", encoding="utf-8")
        with mock.patch.object(device_identity.uuid, "getnode", return_value=HOST_NIC) as getnode:
            self.assertEqual(device_identity._fake_machine_id(str(root)), "adversarial-287-id")
            self.assertEqual(device_identity._fake_machine_id(root), "adversarial-287-id")
            self.assertEqual(device_identity._fake_machine_id(_PathLike(str(root))), "adversarial-287-id")
        getnode.assert_not_called()

    def test_relative_and_empty_roots_of_every_type_fail_closed_without_cwd_writes(self):
        planted = self.cwd / "system" / "drone-app" / "device-id"
        planted.parent.mkdir(parents=True)
        planted.write_text("planted-cwd-id\n", encoding="utf-8")
        before = _snapshot(self.cwd)
        hostile = (
            "",
            ".",
            "relative/root",
            _PathLike(""),
            _PathLike("."),
            _PathLike("relative-userdata"),
        )
        with mock.patch.object(device_identity.uuid, "getnode", return_value=HOST_NIC) as getnode:
            for root in hostile:
                with self.subTest(root=getattr(root, "_value", root)):
                    with self.assertRaises(TypeError):
                        device_identity._fake_machine_id(root)
        getnode.assert_not_called()
        self.assertEqual(planted.read_text(encoding="utf-8"), "planted-cwd-id\n")
        self.assertEqual(_snapshot(self.cwd), before)

    def test_bytes_pathlike_root_fails_closed_before_identity_or_writes(self):
        root = self._root("bytes-root")
        before = _snapshot(self.base)
        with mock.patch.object(device_identity.uuid, "getnode", return_value=HOST_NIC) as getnode:
            with self.assertRaises(TypeError):
                device_identity._fake_machine_id(_PathLike(os.fsencode(str(root))))
        getnode.assert_not_called()
        self.assertEqual(_snapshot(self.base), before)

    def test_mock_root_is_rejected_before_fspath_is_consulted(self):
        root = self._root("mock-root")
        spec_root = mock.MagicMock()
        spec_root.__fspath__.return_value = str(root)
        with mock.patch.object(device_identity.uuid, "getnode", return_value=HOST_NIC) as getnode:
            with self.assertRaises(TypeError):
                device_identity._fake_machine_id(spec_root)
        getnode.assert_not_called()
        spec_root.__fspath__.assert_not_called()
        self.assertFalse((root / "system" / "drone-app" / "device-id").exists())

    def test_absolute_device_id_file_override_wins_for_str_root_and_root_stays_clean(self):
        root = self._root("override-root")
        override = self.base / "elsewhere" / "device-id"
        override.parent.mkdir()
        os.environ["DRONE_DEVICE_ID_FILE"] = str(override)
        with mock.patch.object(device_identity.uuid, "getnode", return_value=HOST_NIC) as getnode:
            value = device_identity._fake_machine_id(str(root))
        getnode.assert_not_called()
        self.assertEqual(override.read_text(encoding="utf-8").strip(), value)
        self.assertFalse((root / "system" / "drone-app" / "device-id").exists())

    def test_settings_from_env_agrees_with_str_helper_for_fake_data(self):
        userdata = self._root("settings-root")
        env = {
            "USE_FAKE_DATA": "1",
            "USERDATA_ROOT": str(userdata),
            "ROMS_ROOT": str(userdata / "roms"),
            "BIOS_ROOT": str(userdata / "bios"),
            "SAVES_ROOT": str(userdata / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(userdata / "state.db"),
            "DRONE_DEVICE_ID_FILE": str(userdata / "system" / "drone-app" / "device-id"),
        }
        with mock.patch.dict("os.environ", env, clear=True):
            settings = Settings.from_env()
        self.assertRegex(settings.device_id, MAC_SHAPE)
        self.assertEqual(device_identity._fake_machine_id(str(userdata)), settings.device_id)


if __name__ == "__main__":
    unittest.main()
