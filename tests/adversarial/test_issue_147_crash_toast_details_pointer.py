"""Adversarial UAT for issue #147: crash toast details must remain readable.

Issue contract: EmulationStation toasts are capped at 80 characters. The
second crash toast must retain the complete ``Details: Debug > Game Crashes.``
pointer, including when its action needs shortening. The ordinary unplug action
for a memory-corruption crash should fit in full for the normal controller
count, while the high-count branch remains capped and actionable.
"""

from __future__ import annotations

import unittest

from app.device.fix_assets import game_crash_notifier


DETAILS = "Details: Debug > Game Crashes."
LIMIT = 80
CONFIG = {"short_session_seconds": 15, "joystick_hint_threshold": 8}


def _memory_crash():
    return game_crash_notifier.assess_launch("stack smashing detected", 5, "", CONFIG)


class Issue147ToastDetailsTests(unittest.TestCase):
    def test_default_unplug_action_and_details_fit_for_normal_controller_count(self):
        # Exercise the ordinary path at both ends of its boundary: no devices
        # and exactly the hint threshold (the count hint starts above it).
        for count in (0, CONFIG["joystick_hint_threshold"]):
            with self.subTest(joystick_count=count):
                first, second = game_crash_notifier.build_messages(
                    "snes", "/userdata/roms/snes/Super Mario World.zip",
                    _memory_crash(), count, CONFIG,
                )
                self.assertLessEqual(len(first), LIMIT)
                self.assertEqual(
                    first,
                    "Super Mario World (snes) crashed: the emulator hit a memory-corruption error.",
                )
                self.assertLessEqual(len(second), LIMIT)
                self.assertEqual(
                    second,
                    "Unplug USB controllers or adapters and relaunch. " + DETAILS,
                )

    def test_long_action_is_shortened_before_the_complete_details_pointer(self):
        verdict = _memory_crash()
        verdict["action"] = "Investigate " + ("controller configuration " * 12)
        _, second = game_crash_notifier.build_messages("snes", "game.zip", verdict, 2, CONFIG)
        self.assertLessEqual(len(second), LIMIT)
        self.assertTrue(second.endswith(" " + DETAILS), second)
        self.assertIn("...", second)
        self.assertNotIn("Game Crashes...", second)

    def test_high_controller_count_preserves_pointer_and_cap(self):
        # The count-specific action must observe the same toast contract.
        for count in (9, 12, 100):
            with self.subTest(joystick_count=count):
                verdict = _memory_crash()
                verdict["action"] = "A deliberately long action " * 8
                _, second = game_crash_notifier.build_messages(
                    "snes", "game.zip", verdict, count, CONFIG,
                )
                self.assertLessEqual(len(second), LIMIT)
                self.assertTrue(second.endswith(" " + DETAILS), second)
                self.assertIn(str(count), second)
                self.assertIn("Unplug some and relaunch.", second)

    def test_plain_backstop_keeps_exact_cap_and_marks_truncation(self):
        exact = "x" * LIMIT
        overflow = "x" * (LIMIT + 1)
        self.assertEqual(game_crash_notifier.toast_text(exact), exact)
        shortened = game_crash_notifier.toast_text(overflow)
        self.assertEqual(len(shortened), LIMIT)
        self.assertTrue(shortened.endswith("..."))


if __name__ == "__main__":
    unittest.main()
