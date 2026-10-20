"""Issue #461: NUL-containing identity roots and override paths fail closed with TypeError.

A NUL byte is never valid in a filesystem path. Before the fix it passed the path-type guard,
reached ``read_text`` and escaped as a raw ``ValueError``. Every entry point must now reject it
with ``TypeError`` before any identity read, generation, or write.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity

HOST_NIC = 0x0242AC110002


class NulIdentityRootTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-461-nul-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        (self.base / "root" / "system" / "drone-app").mkdir(parents=True)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DRONE_DEVICE_ID_FILE", None)

    def _assert_rejected_before_identity(self, call):
        with mock.patch.object(device_identity.uuid, "getnode", return_value=HOST_NIC) as getnode:
            with self.assertRaises(TypeError):
                call()
        getnode.assert_not_called()
        self.assertEqual(
            sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*")),
            ["root", "root/system", "root/system/drone-app"],
        )

    def test_nul_str_root_is_rejected_before_identity_read(self):
        hostile = f"{self.base / 'root'}\x00suffix"
        self._assert_rejected_before_identity(lambda: device_identity._fake_machine_id(hostile))

    def test_nul_pathlike_root_is_rejected_before_identity_read(self):
        hostile = Path(f"{self.base / 'root'}\x00suffix")
        self._assert_rejected_before_identity(lambda: device_identity._fake_machine_id(hostile))

    def test_nul_path_guard_rejects_every_label(self):
        # os.environ cannot hold NUL, so the override label is exercised through the guard itself.
        for value in (f"{self.base}\x00", Path(f"{self.base}\x00")):
            with self.assertRaises(TypeError):
                device_identity._require_real_path(value, "DRONE_DEVICE_ID_FILE")

    def test_clean_absolute_root_still_resolves(self):
        root = self.base / "root"
        self.assertEqual(
            device_identity._device_id_path(root),
            root / "system" / "drone-app" / "device-id",
        )


if __name__ == "__main__":
    unittest.main()
