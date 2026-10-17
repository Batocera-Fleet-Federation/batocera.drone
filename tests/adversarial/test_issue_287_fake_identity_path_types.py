"""Issue #287: accepted filesystem root types must work through identity persistence.

Contract derived from the issue: ``_require_real_path`` accepts strings and
``os.PathLike`` objects, so fake identity must be able to compose the same
userdata paths for each accepted type. The result must remain scoped to that
root, persist across calls, and must not fall back to a host NIC identifier.
"""

from __future__ import annotations

import os
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


class CustomPathLike(os.PathLike):
    def __init__(self, value: str) -> None:
        self.value = value

    def __fspath__(self) -> str:
        return self.value


class FakeIdentityPathTypeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="issue-287-")
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_string_and_custom_pathlike_roots_persist_under_their_own_roots(self) -> None:
        roots = (self.root / "string-root", self.root / "pathlike-root")
        with mock.patch.object(device_identity.uuid, "getnode", return_value=0x0242AC110002) as getnode:
            for root, supplied in zip(roots, (str(roots[0]), CustomPathLike(str(roots[1])))):
                with self.subTest(root_type=type(supplied).__name__):
                    root.mkdir()
                    value = device_identity._fake_machine_id(supplied)
                    persisted_path = root / "system" / "drone-app" / "device-id"
                    self.assertEqual(persisted_path.read_text(encoding="utf-8").strip(), value)
                    self.assertRegex(value, re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"))
                    self.assertNotEqual(value, "02:42:ac:11:00:02")
                    self.assertEqual(device_identity._fake_machine_id(supplied), value)
        getnode.assert_not_called()
        self.assertFalse((self.root / "system").exists(), "identity escaped the supplied root")


if __name__ == "__main__":
    unittest.main()
