"""Unit tests for virtual_keyboard's pure/logic-level pieces -- what's
testable without a live GL/imgui context (imgui.input_text,
imgui.begin_popup_modal, and real gamepad-vs-mouse activation detection
all need an actual created imgui context at minimum, not a plain
unittest.TestCase; that's covered by a real render smoke test instead, not
here -- see ports-client/README.md's Verification status).
"""

import unittest

from ui import virtual_keyboard as vk


class OpenSessionTests(unittest.TestCase):
    def tearDown(self) -> None:
        vk._session = None
        vk._just_opened = False
        vk._last_committed = None

    def test_open_session_sets_expected_state(self) -> None:
        vk._open_session("Password", "hunter2", True)
        self.assertIsNotNone(vk._session)
        self.assertEqual(vk._session.target_label, "Password")
        self.assertEqual(vk._session.live_text, "hunter2")
        self.assertTrue(vk._session.is_password)
        self.assertFalse(vk._session.shift_active)
        self.assertFalse(vk._session.symbols_active)
        self.assertTrue(vk._just_opened)

    def test_open_session_replaces_any_prior_session(self) -> None:
        vk._open_session("Username", "old", False)
        vk._open_session("Password", "new", True)
        self.assertEqual(vk._session.target_label, "Password")
        self.assertEqual(vk._session.live_text, "new")


class DisplayCharTests(unittest.TestCase):
    def test_lowercase_by_default(self) -> None:
        self.assertEqual(vk._display_char("Q", shift_active=False, symbols_active=False), "q")

    def test_uppercase_with_shift(self) -> None:
        self.assertEqual(vk._display_char("Q", shift_active=True, symbols_active=False), "Q")

    def test_symbols_ignore_shift(self) -> None:
        self.assertEqual(vk._display_char("#", shift_active=True, symbols_active=True), "#")
        self.assertEqual(vk._display_char("#", shift_active=False, symbols_active=True), "#")


class SessionMutationTests(unittest.TestCase):
    """The button-click mutations _draw_key_rows performs are one-liners
    against a _Session -- exercised directly here without needing the
    imgui.button() calls that surround them in the real draw path."""

    def tearDown(self) -> None:
        vk._session = None
        vk._just_opened = False
        vk._last_committed = None

    def test_append_and_backspace(self) -> None:
        vk._open_session("Field", "", False)
        session = vk._session
        session.live_text += _display_char_for(session, "H")
        session.live_text += _display_char_for(session, "I")
        self.assertEqual(session.live_text, "hi")
        session.live_text = session.live_text[:-1]
        self.assertEqual(session.live_text, "h")

    def test_shift_toggle_affects_case(self) -> None:
        vk._open_session("Field", "", False)
        session = vk._session
        session.shift_active = not session.shift_active
        self.assertTrue(session.shift_active)
        self.assertEqual(_display_char_for(session, "A"), "A")
        session.shift_active = not session.shift_active
        self.assertEqual(_display_char_for(session, "A"), "a")

    def test_symbols_toggle(self) -> None:
        vk._open_session("Field", "", False)
        session = vk._session
        self.assertFalse(session.symbols_active)
        session.symbols_active = not session.symbols_active
        self.assertTrue(session.symbols_active)


def _display_char_for(session, ch: str) -> str:
    return vk._display_char(ch, shift_active=session.shift_active, symbols_active=session.symbols_active)


if __name__ == "__main__":
    unittest.main()
