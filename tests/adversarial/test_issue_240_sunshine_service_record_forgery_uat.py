"""Issue #240: a service-text digest is not durable ownership provenance.

The UI is allowed to stop or remove only the Sunshine service that Drone itself
created.  The current ownership sidecar contains merely SHA-256(service text).
That value is reproducible by anyone who can create a same-named local service;
it is not a secret, a signature, or installation evidence.  A copied service
plus its calculated public digest must therefore remain administrator-owned.

The test uses no real service manager, network, or Sunshine binary.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from app.multiplayer import models as m
from app.multiplayer.dependencies import SunshineInstaller, service_path, service_record
from multiplayer_support import FakeProbe
from test_multiplayer_core import make_settings


class SunshineServiceRecordForgeryUAT(unittest.TestCase):
    def test_uninstall_rejects_a_service_with_a_recomputed_public_digest_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = make_settings(temporary)
            probe = FakeProbe(settings)
            commands = []

            def run(argv, **_kwargs):
                commands.append(list(argv))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            installer = SunshineInstaller(settings, probe, run=run, arch=lambda: "x86_64")
            # The text and SHA-256 are fully derivable from the program; no Drone
            # installation or managed binary exists in this setup.
            copied = installer._service_text()
            foreign = service_path(settings)
            foreign.parent.mkdir(parents=True)
            foreign.write_text(copied, encoding="utf-8")
            service_record(settings).parent.mkdir(parents=True)
            service_record(settings).write_text(
                hashlib.sha256(copied.encode("utf-8")).hexdigest(), encoding="utf-8"
            )

            with mock.patch("app.multiplayer.dependencies.shutil.which", return_value=None):
                with self.assertRaises(m.MultiplayerError) as caught:
                    installer.uninstall()

            self.assertEqual(caught.exception.code, "not_managed")
            self.assertEqual(commands, [], "a forgeable digest must not authorize service execution")
            self.assertEqual(foreign.read_text(encoding="utf-8"), copied)
            self.assertTrue(service_record(settings).exists(), "foreign provenance evidence must be preserved")


if __name__ == "__main__":
    unittest.main()
