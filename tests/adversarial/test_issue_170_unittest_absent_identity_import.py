"""Adversarial regression for issue #170: identity modules must import without unittest.

The #170 patch added a top-level ``from unittest import mock`` to
``app/common/device_identity.py`` (commit 7982b9f; ``main`` has no such import). The
Drone's production contract is stdlib-only, and ``state_store`` imports ``unittest.mock``
lazily with an ``ImportError`` fallback because the Batocera Python may not ship
``unittest``. The identity modules that the patch touched must keep that contract.

Expected behavior, derived from the repo contract before reading the patch:

- With ``unittest`` unimportable, the state store and the identity modules import.
- A real userdata root is still accepted by the identity path guard (positive control).

Each probe runs in a fresh interpreter with ``sys.modules['unittest']`` blocked so the
test runner's own imports are not affected.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

PROBE = r"""
import sys
sys.modules["unittest"] = None
sys.modules["unittest.mock"] = None
sys.path.insert(0, sys.argv[1])
import tempfile
from pathlib import Path

import app.storage.state_store  # control: already known to be unittest-free
import app.common.device_identity as device_identity
import app.common.identity_provisioning  # noqa: F401 - import must succeed

root = Path(tempfile.mkdtemp())
device_identity._require_real_path(str(root), "userdata_root")
print("identity-imports-ok")
"""


class UnittestAbsentIdentityImportTests(unittest.TestCase):
    def test_identity_modules_import_and_accept_a_real_root_without_unittest(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(
                [sys.executable, "-I", "-c", PROBE, str(REPO_ROOT)],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"identity modules failed to import without unittest:\n{result.stderr}",
        )
        self.assertIn("identity-imports-ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
