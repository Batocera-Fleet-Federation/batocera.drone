"""Issue #240 amendment: a SNES game with no core configured must be playable, and honestly so.

Expected behaviour (derived from the amendment, not from the implementation):

* A core the user chose (``batocera.conf``) is never silently replaced by Drone.
* When nothing is configured Drone may use Batocera's own default core, or an
  installed core, but only one that is really installed, and the session must
  pin exactly that core (for this ROM only) so both machines emulate alike.
* The misleading "No emulator is set ... assuming libretro default" text is gone.
* Nothing is advertised as ready when no usable core exists, and the blocker
  (with a way to fix it) is the first reason shown.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from app.multiplayer import models as m
from app.multiplayer.adapters.retroarch import RetroArchAdapter
from multiplayer_support import RETRO_FILES, FakeProbe
from test_multiplayer_core import make_settings

SNES = {"system": "snes", "rom_name": "Kart.sfc", "players": 2, "title_key": "kart"}
GENERIC = "/usr/share/batocera/configgen/configgen-defaults.yml"
ARCH = "/usr/share/batocera/configgen/configgen-defaults-arch.yml"
INFO = 'display_name = "{n}"\ndisplay_version = "1.0"\nsavestate = "true"\nsavestate_features = "deterministic"\n'


class RetroArchCoreResolutionUAT(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = make_settings(tmp.name)

    def adapter(self, files=None, conf=None):
        probe = FakeProbe(self.settings, binaries={"retroarch": "/usr/bin/retroarch"},
                          versions={"/usr/bin/retroarch": "1.19.1"}, files={**RETRO_FILES, **(files or {})}, conf=conf or {})
        return RetroArchAdapter(probe)

    def test_user_chosen_core_that_is_missing_is_never_swapped_for_another(self):
        # snes9x is installed, but the user explicitly chose bsnes. Silently running a different core would hide the problem.
        result = self.adapter(conf={"snes.core": "bsnes"}).assess_game(SNES)
        self.assertEqual(result["classification"], m.INCOMPATIBLE)
        self.assertEqual(result["emulator"]["core"], "bsnes")
        self.assertFalse(result["ready"])
        self.assertIn("bsnes", result["reasons"][0])

    def test_user_core_beats_batocera_default_and_reports_its_source(self):
        files = {GENERIC: "snes:\n  emulator: libretro\n  core: bsnes\n", "/usr/lib/libretro/bsnes_libretro.so": "x",
                 "/usr/share/libretro/info/bsnes_libretro.info": INFO.format(n="bsnes")}
        result = self.adapter(files=files, conf={"snes.core": "snes9x"}).assess_game(SNES)
        self.assertEqual(result["emulator"]["core"], "snes9x")
        self.assertEqual(result["core_source"], "batocera.conf")

    def test_per_game_core_beats_system_core(self):
        conf = {'snes["Kart.sfc"].core': "snes9x", "snes.core": "bsnes"}
        self.assertEqual(self.adapter(conf=conf).assess_game(SNES)["emulator"]["core"], "snes9x")

    def test_architecture_defaults_override_the_generic_defaults(self):
        files = {GENERIC: "snes:\n  emulator: libretro\n  core: bsnes\n", ARCH: "snes:\n  core: snes9x\n"}
        result = self.adapter(files=files).assess_game(SNES)
        self.assertEqual((result["core_source"], result["emulator"]["core"]), ("batocera_default", "snes9x"))

    def test_other_systems_defaults_do_not_leak_into_snes(self):
        files = {GENERIC: "nes:\n  emulator: libretro\n  core: fceumm\n"}
        result = self.adapter(files=files).assess_game(SNES)
        self.assertEqual(result["emulator"]["core"], "snes9x")
        self.assertEqual(result["core_source"], "installed_candidate")

    def test_default_file_noise_is_tolerated(self):
        noisy = "# header\r\n\r\nsnes:   # comment\r\n\temulator:   'libretro'\r\n\tcore: \"snes9x\"  # x\r\nnes: [broken\r\n"
        result = self.adapter(files={GENERIC: noisy}).assess_game(SNES)
        self.assertEqual(result["emulator"]["core"], "snes9x")
        self.assertEqual(result["classification"], m.LIKELY_COMPATIBLE)

    def test_no_installed_core_is_not_ready_and_blocker_comes_first(self):
        adapter = self.adapter(files={GENERIC: "snes:\n  emulator: libretro\n  core: snes9x\n"})
        adapter.probe.files.pop("/usr/lib/libretro/snes9x_libretro.so")
        adapter.refresh()
        result = adapter.assess_game(SNES)
        self.assertFalse(result["ready"])
        self.assertIn(result["classification"], (m.UNSUPPORTED, m.INCOMPATIBLE))
        self.assertIn("Content Downloader", result["reasons"][0])
        self.assertNotIn("assuming", " ".join(result["reasons"]))

    def test_session_pins_exactly_the_resolved_core_for_this_rom_only(self):
        adapter = self.adapter()
        result = adapter.assess_game(SNES)
        self.assertTrue(result["ready"])
        session = {"game": {"system": "snes", "rom_name": "Kart.sfc"}, "emulator": result["emulator"]}
        edits = adapter.configure_networking(session, m.HOST_ROLE, {"conf_path": "/userdata/system/batocera.conf",
                                                                   "secret": "s3cret", "port": 55435})
        pinned = {edit["key"]: edit["value"] for edit in edits if edit["path"].endswith("batocera.conf")}
        self.assertEqual(pinned['snes["Kart.sfc"].core'], "snes9x")
        self.assertEqual(pinned['snes["Kart.sfc"].emulator'], "libretro")
        self.assertNotIn("snes.core", pinned, "the user's system-wide setting must not be rewritten")
        self.assertNotIn("global.core", pinned)

    def test_non_libretro_default_is_reported_unsupported_not_forced(self):
        files = {GENERIC: "snes:\n  emulator: mesen-s\n  core: mesen-s\n"}
        result = self.adapter(files=files).assess_game(SNES)
        self.assertEqual(result["classification"], m.UNSUPPORTED)
        self.assertFalse(result["ready"])

    def test_single_player_metadata_still_rules_netplay_out(self):
        result = self.adapter().assess_game({**SNES, "players": 1})
        self.assertFalse(result["ready"])
        self.assertIn("single player", result["reasons"][0])


if __name__ == "__main__":
    unittest.main()
