"""Detection against the REAL machine running the tests (not simulated).

On a development machine these mostly skip: they only assert anything about an emulator
that is actually installed. On a Batocera device (or any host with the binaries) they check
that real detection returns coherent, honest answers. Passing here is the closest an
automated test gets to "the adapter works with the installed build"; a real two-machine
session still has to be played by a person (see docs/multiplayer.md, "Validation status").
"""

import shutil
import tempfile
import unittest

from app.multiplayer.adapters import adapters_by_id, build_adapters
from app.multiplayer.environment import SystemProbe
from test_multiplayer_core import make_settings


class RealEnvironmentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = make_settings(tmp.name)
        self.probe = SystemProbe(self.settings)
        self.adapters = adapters_by_id(build_adapters(self.probe))

    def test_every_adapter_can_describe_itself_on_this_machine(self):
        for adapter in self.adapters.values():
            with self.subTest(adapter=adapter.id):
                document = adapter.capabilities()
                self.assertIn(document["readiness"]["status"], ("ready", "manual_steps_required", "configuration_required", "not_installed", "unsupported_build"))
                if not document["installed"]:
                    self.assertIn(document["readiness"]["status"], ("not_installed", "manual_steps_required"))

    @unittest.skipUnless(shutil.which("retroarch"), "RetroArch is not installed here")
    def test_retroarch_reports_a_version_and_honest_automation(self):
        adapter = self.adapters["retroarch"]
        self.assertTrue(adapter.detect_installed())
        self.assertTrue(adapter.detect_version(), "retroarch --version produced no version")
        self.assertIn(adapter.effective_automation(), ("full", "manual"))

    @unittest.skipUnless(shutil.which("dolphin-emu"), "Dolphin is not installed here")
    def test_dolphin_reports_a_version(self):
        self.assertTrue(self.adapters["dolphin"].detect_version())

    @unittest.skipUnless(shutil.which("sunshine"), "Sunshine is not installed here")
    def test_sunshine_host_readiness_is_detected(self):
        self.assertTrue(self.adapters["streaming"].detect_features()["REMOTE_STREAMING"]["host_ready"])

    def test_controller_detection_never_raises(self):
        self.assertIsInstance(self.probe.controllers(), list)
        self.assertIn("cpu_count", self.probe.hardware())


if __name__ == "__main__":
    unittest.main()
