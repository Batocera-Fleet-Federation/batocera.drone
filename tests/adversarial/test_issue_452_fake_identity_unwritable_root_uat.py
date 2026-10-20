"""Issue #452 UAT: fake-data identity must never be host-derived, even when the root is unwritable.

Before #452, ``_fake_machine_id`` on an absolute root that could not be persisted fell back to
the NIC-derived id from ``uuid.getnode()``. That breaks the #199/#227 invariant. The fix routes
fake-data callers to a stable, root-path-derived id. Real-device ``_machine_id`` keeps its
NIC fallback on purpose so the id does not change on every start.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.common import device_identity  # noqa: E402

HOST_NODE = 0x0242AC110002
HOST_DERIVED = "02:42:ac:11:00:02"
MAC_SHAPED = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class FakeIdentityUnwritableRootTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.blocker = self.root / "blocker"
        self.blocker.write_text("not a directory", encoding="utf-8")
        self._getnode = mock.patch.object(device_identity.uuid, "getnode", return_value=HOST_NODE)
        self._getnode.start()
        self._nic = mock.patch.object(device_identity, "_physical_mac_candidates", return_value=[HOST_DERIVED])
        self._nic.start()

    def tearDown(self):
        self._nic.stop()
        self._getnode.stop()
        self._tmp.cleanup()

    def test_unwritable_root_never_returns_host_nic_id(self):
        value = device_identity._fake_machine_id(self.blocker / "userdata")
        self.assertNotEqual(value, HOST_DERIVED)
        self.assertRegex(value, MAC_SHAPED)

    def test_unwritable_root_id_is_stable_across_starts(self):
        first = device_identity._fake_machine_id(self.blocker / "userdata")
        second = device_identity._fake_machine_id(self.blocker / "userdata")
        self.assertEqual(first, second)

    def test_unwritable_roots_get_distinct_ids(self):
        other = self.root / "other-blocker"
        other.write_text("x", encoding="utf-8")
        self.assertNotEqual(
            device_identity._fake_machine_id(self.blocker / "userdata"),
            device_identity._fake_machine_id(other / "userdata"),
        )

    def test_real_device_machine_id_keeps_nic_fallback_when_unpersistable(self):
        # Intentional: real installs keep the documented NIC fallback (see _machine_id comment).
        self.assertEqual(device_identity._machine_id(self.blocker / "userdata"), HOST_DERIVED)

    def test_writable_root_persists_random_id_not_host_derived(self):
        userdata = self.root / "userdata"
        (userdata / "system" / "drone-app").mkdir(parents=True)
        first = device_identity._fake_machine_id(userdata)
        self.assertNotEqual(first, HOST_DERIVED)
        self.assertEqual(device_identity._fake_machine_id(userdata), first)


if __name__ == "__main__":
    unittest.main()
