"""Adversarial regression for issue #170: identity reset must work in flat-mode entrypoints.

The Drone runs in two import modes (CLAUDE.md, "Web app + package layout"): the package
``app.<module>`` form used by tests, and the flat ``python app/main.py`` form where each
subpackage is a top-level package. In flat mode a relative import such as
``from ..common.device_identity import ...`` cannot climb above the top-level ``transfer``
package and raises ``ImportError``.

The #170 patch added ``from ..common.device_identity import _is_mock_path, _require_real_path``
inside ``transfer/swarm_reset._clear_pinned_peer_certificates``. The identity provisioning
journal relies on that function, so a flat-mode clone would fail before reset completes.
The 163 real-boot UAT log shows ``Identity provisioning did not complete (ImportError)``.

Expected behavior, derived from the flat-mode contract before reading the patch:

- In flat mode, the swarm reset and identity modules import and clear pins for a real root.
- The pin-clearing path returns normally for a real userdata root with no pins (control).

The probe runs in a fresh interpreter with ``app/`` on ``sys.path`` (flat mode), from an
empty temporary cwd, so the test runner's own import mode is not affected.
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
import tempfile
import types
sys.path.insert(0, sys.argv[1])

from storage import state_store  # control: flat import of the store
from common import identity_provisioning  # flat import of the identity provisioner
from transfer import swarm_reset

root = tempfile.mkdtemp()
cleared = swarm_reset._clear_pinned_peer_certificates(types.SimpleNamespace(userdata_root=root))
assert cleared == 0, cleared
print("flat-swarm-reset-ok")
"""


class FlatModeSwarmResetImportTests(unittest.TestCase):
    def test_swarm_reset_and_identity_modules_work_in_flat_mode(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(
                [sys.executable, "-I", "-c", PROBE, str(REPO_ROOT / "app")],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"flat-mode swarm reset failed:\n{result.stderr}",
        )
        self.assertIn("flat-swarm-reset-ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
