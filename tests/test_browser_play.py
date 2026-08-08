import unittest

from app.roms.browser_play import SYSTEM_CORE_MAP, browser_play_core_for_system


class BrowserPlayCoreMapTests(unittest.TestCase):
    def test_known_systems_map_to_expected_cores(self) -> None:
        self.assertEqual(browser_play_core_for_system("nes"), "fceumm")
        self.assertEqual(browser_play_core_for_system("snes"), "snes9x")
        self.assertEqual(browser_play_core_for_system("gba"), "mgba")
        self.assertEqual(browser_play_core_for_system("psx"), "mednafen_psx_hw")
        self.assertEqual(browser_play_core_for_system("megadrive"), "genesis_plus_gx")
        self.assertEqual(browser_play_core_for_system("mastersystem"), "genesis_plus_gx")
        self.assertEqual(browser_play_core_for_system("gamegear"), "genesis_plus_gx")

    def test_is_case_insensitive(self) -> None:
        self.assertEqual(browser_play_core_for_system("SNES"), "snes9x")
        self.assertEqual(browser_play_core_for_system(" Gba "), "mgba")

    def test_unsupported_or_empty_system_returns_none(self) -> None:
        self.assertIsNone(browser_play_core_for_system("dreamcast"))
        self.assertIsNone(browser_play_core_for_system("ps2"))
        self.assertIsNone(browser_play_core_for_system(""))
        self.assertIsNone(browser_play_core_for_system(None))

    def test_map_values_have_no_stray_whitespace_or_casing(self) -> None:
        for system, core in SYSTEM_CORE_MAP.items():
            self.assertEqual(system, system.strip().lower())
            self.assertEqual(core, core.strip())


if __name__ == "__main__":
    unittest.main()
