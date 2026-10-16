"""Issue #286: custom PathLike roots must obey the absolute userdata boundary."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity


class RelativePathLike(os.PathLike):
    """A valid PathLike whose value resolves relative to the process cwd."""

    def __fspath__(self):
        return "relative-userdata"


class InvalidPathLike(os.PathLike):
    """Malformed PathLike implementation; pathlib requires __fspath__ to return str/bytes."""

    def __fspath__(self):
        return None


class RelativePathLikeRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.cwd = self.base / "cwd"
        self.cwd.mkdir()
        self.previous_cwd = Path.cwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, self.previous_cwd)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DRONE_DEVICE_ID_FILE", None)

    def test_relative_custom_pathlike_is_rejected_before_read_or_write(self):
        planted = self.cwd / "system" / "drone-app" / "device-id"
        planted.parent.mkdir(parents=True)
        planted.write_text("planted-cwd-id\n", encoding="utf-8")

        for operation in (
            device_identity._fake_machine_id,
            device_identity._machine_id,
            device_identity._device_id_path,
            device_identity._read_persisted_machine_id,
            lambda root: device_identity._write_persisted_machine_id(root, "new-device-id"),
        ):
            with self.subTest(operation=getattr(operation, "__name__", "write")):
                with self.assertRaises(TypeError):
                    operation(RelativePathLike())
                self.assertEqual(planted.read_text(encoding="utf-8"), "planted-cwd-id\n")
                self.assertEqual(
                    sorted(str(path.relative_to(self.cwd)) for path in self.cwd.rglob("*")),
                    ["system", "system/drone-app", "system/drone-app/device-id"],
                )

    def test_malformed_pathlike_is_rejected_without_cwd_side_effects(self):
        for operation in (device_identity._fake_machine_id, device_identity._machine_id):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(TypeError):
                    operation(InvalidPathLike())
        self.assertEqual(list(self.cwd.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
