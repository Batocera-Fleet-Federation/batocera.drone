"""Out of scope for #169 (report separately): the identity hardware-change lock is written under cwd.

Evidence, from a live probe on this checkout: ``identity_provisioning.cancel_hardware_change``
and ``authorize_hardware_change`` build the identity directory from ``Path(settings.userdata_root)``.
With ``MagicMock(spec=Path)`` that wrap resolves to ``.``, so the lock file
``./system/drone-app/identity/provisioning.lock`` is created in the working directory. No SQLite
is involved, so this is file-level pollution rather than the state DB that issue #169 names.

The existing ``adversarial-issue-169-out-of-scope-cwd-identity-files`` suite covers device-id
retire, device-id choose, and pinned-certificate unlink. It does not cover this lock path.

Expected behavior if it were in scope: a spec'd root creates nothing under the working directory.
These tests are informational for the worker and are deliberately not part of the #169 blocking gate.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning


class HardwareChangeLockSpecRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmpdir.name).resolve()
        os.chdir(self.cwd)

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._tmpdir.cleanup()
        self._env.stop()

    def test_cancel_hardware_change_does_not_create_lock_under_cwd(self) -> None:
        settings = SimpleNamespace(userdata_root=mock.MagicMock(spec=Path))
        try:
            identity_provisioning.cancel_hardware_change(settings)
        except (TypeError, ValueError, OSError):
            pass
        self.assertFalse(
            (self.cwd / "system" / "drone-app" / "identity" / "provisioning.lock").exists(),
            msg="cancel_hardware_change created the identity lock under the working directory",
        )

    def test_authorize_hardware_change_does_not_create_lock_under_cwd(self) -> None:
        settings = SimpleNamespace(userdata_root=mock.create_autospec(Path, instance=True))
        try:
            identity_provisioning.authorize_hardware_change(settings)
        except (TypeError, ValueError, OSError):
            pass
        self.assertFalse(
            (self.cwd / "system").exists(),
            msg="authorize_hardware_change created a system/ tree under the working directory",
        )


if __name__ == "__main__":
    unittest.main()
