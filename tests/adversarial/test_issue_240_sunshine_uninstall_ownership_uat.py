"""Issue #240: the UI must never act on an administrator-owned service.

The one-click Sunshine feature owns only the application and service that it
created.  A pre-existing ``DRONE_SUNSHINE`` script can belong to an
administrator or a previous non-Drone setup.  Selecting Uninstall in that
state must fail closed before it asks Batocera to stop the service or executes
the script itself.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from app.multiplayer import models as m
from app.multiplayer.dependencies import SunshineInstaller, service_path
from multiplayer_support import FakeProbe
from test_multiplayer_core import make_settings


class SunshineUninstallOwnershipUAT(unittest.TestCase):
    def test_foreign_service_without_managed_install_is_never_stopped_or_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = make_settings(temporary)
            probe = FakeProbe(settings)
            foreign = service_path(settings)
            foreign.parent.mkdir(parents=True)
            foreign.write_text("#!/bin/sh\necho administrator-owned\n", encoding="utf-8")
            commands = []

            def run(argv, **_kwargs):
                commands.append(list(argv))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            installer = SunshineInstaller(settings, probe, run=run, arch=lambda: "x86_64")

            with self.assertRaises(m.MultiplayerError) as caught:
                installer.uninstall()

            self.assertEqual(caught.exception.code, "not_managed")
            self.assertEqual(commands, [], "uninstall must not invoke an unowned service")
            self.assertEqual(foreign.read_text(encoding="utf-8"), "#!/bin/sh\necho administrator-owned\n")


if __name__ == "__main__":
    unittest.main()
