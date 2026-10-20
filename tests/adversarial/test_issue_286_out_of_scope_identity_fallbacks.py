"""Out-of-scope evidence found while testing #286: identity fallbacks that #286 does not cover.

These are NOT blocking. Each assertion is marked ``expectedFailure`` because the behavior is
outside the #286 relative-root fix and is filed as a separate issue. The suite stays enabled so
the evidence is retained and re-runs on schedule; when the separate issue lands, the owner removes
the decorator.

Expected, from the fake-data identity invariant (#199, #227):

* Fake-data mode must never hand out the host-derived ``uuid.getnode()`` id, even when the
  absolute ``USERDATA_ROOT`` cannot be written. Today ``_machine_id`` falls back to the NIC-derived
  id, which in fake mode is exactly the host-derived value #199 and #227 forbid.
* ``_require_real_path`` accepts ``str`` roots. ``_fake_machine_id`` must therefore either accept an
  absolute ``str`` root or reject it with a clean ``TypeError``. Today it crashes inside
  ``_device_id_path`` with an unrelated ``unsupported operand`` error.
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

HOST_DERIVED = "02:42:ac:11:00:02"
MAC_SHAPED = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class OutOfScopeIdentityFallbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._getnode = mock.patch.object(device_identity.uuid, "getnode", return_value=0x0242AC110002)
        self._getnode.start()

    def tearDown(self):
        self._getnode.stop()
        self._tmp.cleanup()

    # Fixed by #452: fake-data mode no longer falls back to the NIC-derived id.
    def test_fake_unwritable_absolute_root_never_returns_host_derived_id(self):
        # A regular file blocks mkdir for root/…, so the id cannot be persisted under the root.
        blocker = self.root / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        value = device_identity._fake_machine_id(blocker / "userdata")
        self.assertNotEqual(value, HOST_DERIVED)

    # Adjudicated (#287 dispute): the str-root crash is fixed by #287, so this assertion now
    # passes and is kept as a regression instead of an expected failure.
    def test_fake_absolute_str_root_is_accepted_or_cleanly_rejected(self):
        userdata = self.root / "userdata"
        (userdata / "system" / "drone-app").mkdir(parents=True)
        try:
            value = device_identity._fake_machine_id(str(userdata))
        except TypeError as error:
            self.fail(f"absolute str root crashed instead of being accepted: {error}")
        self.assertRegex(value, MAC_SHAPED)


if __name__ == "__main__":
    unittest.main()
