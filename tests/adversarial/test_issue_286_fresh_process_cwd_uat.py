"""Issue #286 UAT in a fresh process: relative roots must not persist into its cwd."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


class FreshProcessCwdIdentityTests(unittest.TestCase):
    def test_empty_and_dot_paths_are_rejected_without_creating_cwd_state(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            script = """
from pathlib import Path
from unittest import mock
from app.common import device_identity

with mock.patch.object(device_identity.uuid, 'getnode', return_value=0x0242AC110002):
    for root in (Path(''), Path('.')):
        try:
            device_identity._fake_machine_id(root)
        except TypeError:
            continue
        raise AssertionError(f'relative userdata root accepted: {root!r}')
"""
            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO)
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(cwd.iterdir()), [], "fresh process persisted identity under cwd")


if __name__ == "__main__":
    unittest.main()
