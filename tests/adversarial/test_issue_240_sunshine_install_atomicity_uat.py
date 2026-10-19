"""Issue #240 amendment: Sunshine UI installation must fail without a partial install.

The amendment promises a UI action which installs Sunshine into Drone-owned
storage while preserving anything it does not own.  A conflicting existing
``DRONE_SUNSHINE`` service is a realistic interrupted/administrator-managed
state.  It must be detected before the downloaded application is promoted:
otherwise the UI reports failure but makes a subsequent install impossible
(``already_installed``), while the foreign service still cannot be replaced.

This uses a deterministic fake release/AppImage and never performs network,
service-manager, or emulator work.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from app.multiplayer.dependencies import (DOWNLOAD_PREFIX, SERVICE_NAME,
                                          SunshineInstaller, managed_root,
                                          service_path)
from multiplayer_support import FakeProbe
from test_multiplayer_core import make_settings


IMAGE = b"deterministic-sunshine-appimage"
ASSET_NAME = "Sunshine_2026.1.1_x86_64.AppImage"


class SunshineInstallAtomicityUAT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = make_settings(temporary.name)
        self.probe = FakeProbe(self.settings)

    def installer(self):
        digest = hashlib.sha256(IMAGE).hexdigest()

        def fetch(_url):
            return {"tag_name": "v2026.1.1", "assets": [{
                "name": ASSET_NAME,
                "browser_download_url": f"{DOWNLOAD_PREFIX}v2026.1.1/{ASSET_NAME}",
                "size": len(IMAGE),
                "digest": f"sha256:{digest}",
            }]}

        def download(_url, destination, _size, progress):
            destination.write_bytes(IMAGE)
            progress(len(IMAGE))
            return digest

        def run(argv, **kwargs):
            if argv[-1] == "--appimage-extract":
                extracted = Path(kwargs["cwd"]) / "squashfs-root"
                (extracted / "usr" / "bin").mkdir(parents=True)
                (extracted / "AppRun").write_text("#!/bin/sh\n", encoding="utf-8")
                (extracted / "usr" / "bin" / "sunshine").write_text("binary", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return SunshineInstaller(self.settings, self.probe, fetch_json=fetch,
                                 download=download, run=run, arch=lambda: "x86_64",
                                 spawn=lambda work, _name: work(), sleep=lambda _seconds: None)

    def test_foreign_service_conflict_leaves_no_partial_managed_install(self):
        foreign = service_path(self.settings)
        foreign.parent.mkdir(parents=True)
        foreign.write_text("#!/bin/sh\necho administrator-owned\n", encoding="utf-8")

        installer = self.installer()
        installer.start_install()

        self.assertEqual(installer.job()["state"], "failed")
        self.assertEqual(foreign.read_text(encoding="utf-8"), "#!/bin/sh\necho administrator-owned\n")
        self.assertFalse(
            managed_root(self.settings).exists(),
            "a rejected install must not strand a Drone-managed application without its service",
        )
        self.assertTrue(installer.status()["can_install"], "the UI must offer a retry after the conflict is resolved")
        self.assertEqual(foreign.name, SERVICE_NAME)


if __name__ == "__main__":
    unittest.main()
