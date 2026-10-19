"""Issue #240: a comment alone is not proof Drone owns a Sunshine service.

The one-click UI may stop or remove only a service it created.  An
administrator-owned script can legitimately include the public wording used by
Drone's generated service (for example after copying its documentation).  A
plain marker match must not turn that script into an executable uninstall
target.
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
from app.multiplayer.dependencies import SERVICE_MARKER, SunshineInstaller, service_path
from multiplayer_support import FakeProbe
from test_multiplayer_core import make_settings


class SunshineServiceProvenanceUAT(unittest.TestCase):
    def test_uninstall_rejects_a_foreign_service_that_copies_the_public_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = make_settings(temporary)
            probe = FakeProbe(settings)
            foreign = service_path(settings)
            foreign.parent.mkdir(parents=True)
            original = f"#!/bin/sh\n# {SERVICE_MARKER}\necho administrator-owned\n"
            foreign.write_text(original, encoding="utf-8")
            commands = []

            def run(argv, **_kwargs):
                commands.append(list(argv))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            installer = SunshineInstaller(settings, probe, run=run, arch=lambda: "x86_64")

            with self.assertRaises(m.MultiplayerError) as caught:
                installer.uninstall()

            self.assertEqual(caught.exception.code, "not_managed")
            self.assertEqual(commands, [], "a copied comment must not authorize service execution")
            self.assertEqual(foreign.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
