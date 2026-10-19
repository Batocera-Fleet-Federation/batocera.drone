"""Issue #240 amendment: the Sunshine card may only offer actions that can succeed, and install must recover.

Expected behaviour (derived from the amendment: "install sunshine on the device from the UI ...
everything required is installed and working"):

* ``can_start`` is true only when Drone can really start that Sunshine.  A Sunshine that
  Drone did not install, or whose ``DRONE_SUNSHINE`` name is held by a foreign service, is
  started from Batocera; a button that always answers 409 is a dead end.
* A first Install click after an interrupted earlier install (power loss on a handheld is
  routine) succeeds instead of failing with a bare "File exists".
* Failure messages never leak raw OS error strings without telling the person what to do.
"""
from __future__ import annotations

import hashlib
import json
import os
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
from app.multiplayer.dependencies import SunshineInstaller, managed_binary, managed_root, service_path
from multiplayer_support import FakeProbe
from test_multiplayer_core import make_settings
from test_multiplayer_dependencies import InstallerCase


class SunshineUiActionsUAT(InstallerCase):
    def test_start_is_not_offered_for_a_sunshine_drone_did_not_install(self):
        self.probe.binaries["sunshine"] = "/usr/bin/sunshine"
        installer = self.installer()
        status = installer.status()
        self.assertTrue(status["installed"] and not status["managed"] and not status["running"])
        with self.assertRaises(m.MultiplayerError):
            installer.start_service()
        self.assertFalse(status["can_start"], "the UI must not offer a Start button that always fails")
        self.assertFalse(status["can_install"])
        self.assertFalse(status["can_uninstall"])

    def test_start_is_not_offered_when_a_foreign_service_holds_the_name(self):
        installer = self.installer()
        installer.start_install()
        self.probe.processes.clear()
        script = service_path(self.settings)
        script.write_text("#!/bin/sh\necho administrator-owned\n")
        status = self.installer().status()
        self.assertTrue(status["managed"] and not status["running"])
        with self.assertRaises(m.MultiplayerError):
            self.installer().start_service()
        self.assertFalse(status["can_start"])

    def test_start_is_offered_for_a_drone_install_that_is_stopped(self):
        installer = self.installer()
        installer.start_install()
        self.probe.processes.clear()
        self.assertTrue(self.installer().status()["can_start"])

    def test_first_install_after_an_interrupted_install_succeeds(self):
        root = managed_root(self.settings)
        root.mkdir(parents=True)
        key = root / ".service-key"
        key.write_bytes(b"k" * 32)
        os.chmod(key, 0o600)
        (root / "version.json").write_text("{}")
        installer = self.installer()
        installer.start_install()
        self.assertEqual(installer.job()["state"], "succeeded", installer.job())
        self.assertTrue(installer.is_managed_install())
        self.assertTrue(installer._owns_service())

    def test_failed_install_leaves_no_staging_or_partial_directories(self):
        installer = self.installer(download=lambda url, dest, size, progress: (_ for _ in ()).throw(OSError(28, "No space left on device")))
        installer.start_install()
        self.assertEqual(installer.job()["state"], "failed")
        parent = managed_root(self.settings).parent
        self.assertEqual(list(parent.iterdir()) if parent.exists() else [], [])
        self.assertFalse(service_path(self.settings).exists())

    def test_install_can_be_retried_after_a_failure(self):
        calls = {"n": 0}

        def flaky(url, destination, size, progress):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("connection reset")
            return self.fake_download(url, destination, size, progress)

        installer = self.installer(download=flaky)
        installer.start_install()
        self.assertEqual(installer.job()["state"], "failed")
        installer.start_install()
        self.assertEqual(installer.job()["state"], "succeeded", installer.job())
        self.assertTrue(installer.status()["can_uninstall"])

    def test_untrusted_release_text_never_reaches_a_command_line(self):
        evil = "Sunshine_1.0_x86_64.AppImage"
        document = {"tag_name": "v1; rm -rf /", "assets": [{"name": evil, "size": 19,
                    "browser_download_url": "https://github.com/LizardByte/Sunshine/releases/download/x/" + evil,
                    "digest": "sha256:" + hashlib.sha256(b"fake-appimage-bytes").hexdigest()}]}
        installer = self.installer(fetch_json=lambda url: document)
        installer.start_install()
        self.assertEqual(installer.job()["state"], "succeeded", installer.job())
        for argv in self.commands:
            self.assertFalse(any("rm -rf" in part for part in argv))
        recorded = json.loads((managed_root(self.settings) / "version.json").read_text())
        self.assertEqual(recorded["asset"], evil)
        self.assertNotIn("\n", recorded["tag"])


if __name__ == "__main__":
    unittest.main()
