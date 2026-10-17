"""Issue #287 out-of-scope evidence: a NUL-containing absolute root escapes as a raw ValueError.

Out of scope for #287 (the join fix): the failure happens when identity reads the persisted
file, after the join succeeded, and it reproduces identically for ``pathlib.Path`` roots. The
expected behavior is derived from the existing guard contract: a root that is not a usable
filesystem path is rejected with ``TypeError`` before any identity read, generation, or write.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity

HOST_NIC = 0x0242AC110002


class NulRootOutOfScopeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-287-nul-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        (self.base / "root" / "system" / "drone-app").mkdir(parents=True)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DRONE_DEVICE_ID_FILE", None)

    def test_nul_containing_absolute_str_root_is_rejected_as_a_non_path(self):
        hostile = f"{self.base / 'root'}\x00suffix"
        with mock.patch.object(device_identity.uuid, "getnode", return_value=HOST_NIC) as getnode:
            with self.assertRaises(TypeError):
                device_identity._fake_machine_id(hostile)
        getnode.assert_not_called()
        self.assertEqual(sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*")), ["root", "root/system", "root/system/drone-app"])


if __name__ == "__main__":
    unittest.main()
