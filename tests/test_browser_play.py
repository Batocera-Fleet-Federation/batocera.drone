import unittest

from app.roms.browser_play import (
    ROMSET_SENSITIVE_SYSTEMS,
    SYSTEM_CORE_MAP,
    browser_play_core_for_system,
    browser_play_is_romset_sensitive,
)


class BrowserPlayCoreMapTests(unittest.TestCase):
    def test_known_systems_map_to_expected_cores(self) -> None:
        self.assertEqual(browser_play_core_for_system("nes"), "fceumm")
        self.assertEqual(browser_play_core_for_system("snes"), "snes9x")
        self.assertEqual(browser_play_core_for_system("gba"), "mgba")
        self.assertEqual(browser_play_core_for_system("psx"), "mednafen_psx_hw")
        self.assertEqual(browser_play_core_for_system("megadrive"), "genesis_plus_gx")
        self.assertEqual(browser_play_core_for_system("mastersystem"), "genesis_plus_gx")
        self.assertEqual(browser_play_core_for_system("gamegear"), "genesis_plus_gx")
        self.assertEqual(browser_play_core_for_system("fba"), "fbneo")
        self.assertEqual(browser_play_core_for_system("fbneo"), "fbneo")

    def test_mame_is_deliberately_unsupported(self) -> None:
        # Not just "romset sensitive" -- confirmed live that a modern full
        # romset (MAME 0.265-era) is too far from the vendored
        # mame2003_plus core's ~0.78-era expectations for the button to be
        # worth showing at all. See the module docstring.
        self.assertIsNone(browser_play_core_for_system("mame"))
        self.assertNotIn("mame", SYSTEM_CORE_MAP)

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

    def test_romset_sensitive_systems_are_flagged(self) -> None:
        self.assertTrue(browser_play_is_romset_sensitive("fba"))
        self.assertTrue(browser_play_is_romset_sensitive("fbneo"))
        self.assertTrue(browser_play_is_romset_sensitive(" FBA "))

    def test_non_arcade_systems_are_not_flagged_romset_sensitive(self) -> None:
        self.assertFalse(browser_play_is_romset_sensitive("snes"))
        self.assertFalse(browser_play_is_romset_sensitive("psx"))
        self.assertFalse(browser_play_is_romset_sensitive(""))
        self.assertFalse(browser_play_is_romset_sensitive(None))

    def test_mame_is_not_flagged_romset_sensitive_because_its_not_offered_at_all(self) -> None:
        self.assertFalse(browser_play_is_romset_sensitive("mame"))

    def test_romset_sensitive_systems_are_all_real_map_entries(self) -> None:
        self.assertTrue(ROMSET_SENSITIVE_SYSTEMS.issubset(SYSTEM_CORE_MAP.keys()))


if __name__ == "__main__":
    unittest.main()
