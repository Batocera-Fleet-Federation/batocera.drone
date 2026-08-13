"""An on-screen keyboard for gamepad-only text entry -- Dear ImGui has no
built-in one. Auto-opens when a text field is activated via gamepad (never
via mouse -- mouse+keyboard users keep typing directly through the real
``imgui.input_text`` widget, completely unaffected).

State model: a single module-level "current session," not a per-widget-ID
dict. A ``begin_popup_modal`` blocks all other interaction while open, so
at most one session can ever be active at a time -- a dict keyed by every
possible field would be unneeded complexity for something that is
structurally always 0-or-1 sessions.

Sessions are matched by the exact ImGui label string a call site passes
in, not by ``imgui.get_item_id()``'s int ID: the label is available both
before and after the widget call each frame (the ID isn't, until after),
and every existing call site already has a unique label by ImGui's own
ID-stack rules.

Call sites use ``input_text()`` as a drop-in replacement for
``imgui.input_text`` -- same signature, same return contract -- and
``draw_if_open()`` is called once per frame from ``ui/app.py``'s
``_run_loop``, after every screen has drawn (so a same-frame
``open_popup()`` this widget triggers is followed later in the same frame
by the matching ``begin_popup_modal()`` -- a supported ImGui pattern, no
frame-delay glue needed).
"""

from typing import Optional, Tuple

from imgui_bundle import imgui

_LETTER_ROWS = ("QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM")
_SYMBOL_ROWS = ("1234567890", "-_.@/:;#$%", "!?+=*&()")
_POPUP_NAME = "Virtual Keyboard"


class _Session:
    def __init__(self, label: str, initial_value: str, is_password: bool):
        self.target_label = label
        self.live_text = initial_value
        self.is_password = is_password
        self.shift_active = False
        self.symbols_active = False


_session: Optional[_Session] = None
_just_opened = False
# One-shot: the field this was committed to reads it back exactly once via
# input_text(), then it's cleared -- never a lingering "last value" that
# could bleed into a different field sharing a coincidentally similar name.
_last_committed: Optional[Tuple[str, str]] = None


def input_text(label: str, value: str, flags: int = 0) -> Tuple[bool, str]:
    """Drop-in replacement for ``imgui.input_text`` -- draws the real
    widget completely unchanged (so mouse-click and physical-keyboard
    typing behavior is untouched structurally, not just gated by a flag
    check), and opens this keyboard only when the activation that just
    happened was gamepad-sourced.
    """
    global _last_committed

    committed_this_frame = False
    if _last_committed is not None and _last_committed[0] == label:
        value = _last_committed[1]
        _last_committed = None
        committed_this_frame = True

    changed, new_value = imgui.input_text(label, value, flags)

    if imgui.is_item_activated():
        ctx = imgui.get_current_context()
        # imgui.internal is marked "private API" in the stub but is real
        # and importable; unverified on a physical controller (nothing in
        # dev environments here has one -- see README's Verification
        # status). If active_id_source ever misbehaves on real hardware,
        # the concrete fallback is the public-API-only
        # `imgui.is_item_activated() and not imgui.is_mouse_clicked(0)` --
        # it can't distinguish gamepad from a keyboard Tab-then-Enter
        # activation, an acceptable minor false positive (an extra keyboard
        # popup), not a correctness bug.
        if ctx.active_id_source == imgui.internal.InputSource.gamepad:
            _open_session(label, new_value, bool(flags & imgui.InputTextFlags_.password.value))

    return (changed or committed_this_frame, new_value)


def _open_session(label: str, initial_value: str, is_password: bool) -> None:
    global _session, _just_opened
    _session = _Session(label, initial_value, is_password)
    _just_opened = True


def draw_if_open() -> None:
    """Call exactly once per frame. No-op unless a session is active."""
    global _session, _just_opened, _last_committed

    if _session is None:
        return

    if _just_opened:
        imgui.open_popup(_POPUP_NAME)
        _just_opened = False

    # begin_popup_modal's End is conditional on Begin's return (unlike
    # plain imgui.begin()/end()) -- only call end_popup() inside the
    # `if opened:` branch below, matching every other Begin*Popup*/End*
    # pair in Dear ImGui.
    opened, _ = imgui.begin_popup_modal(_POPUP_NAME, flags=imgui.WindowFlags_.always_auto_resize.value)
    if not opened:
        return

    session = _session
    preview = ("*" * len(session.live_text)) if session.is_password else session.live_text
    imgui.text(preview or " ")
    imgui.spacing()
    imgui.separator()
    imgui.spacing()

    _draw_key_rows(session)

    imgui.spacing()
    cancel_pressed = imgui.button("Cancel") or imgui.is_key_pressed(imgui.Key.gamepad_face_right)
    imgui.same_line()
    done_pressed = imgui.button("Done")

    if cancel_pressed:
        # Discard: leave the field's value exactly as it was before this
        # session opened -- nothing is written to _last_committed.
        _session = None
        imgui.close_current_popup()
    elif done_pressed:
        _last_committed = (session.target_label, session.live_text)
        _session = None
        imgui.close_current_popup()

    imgui.end_popup()


def _display_char(ch: str, *, shift_active: bool, symbols_active: bool) -> str:
    """Pure, unit-testable: what a key labeled `ch` (always uppercase in
    _LETTER_ROWS/_SYMBOL_ROWS) should actually insert/display given the
    current Shift/123 toggle state."""
    if symbols_active:
        return ch
    return ch if shift_active else ch.lower()


def _draw_key_rows(session: _Session) -> None:
    rows = _SYMBOL_ROWS if session.symbols_active else _LETTER_ROWS
    for row in rows:
        for ch in row:
            display = _display_char(ch, shift_active=session.shift_active, symbols_active=session.symbols_active)
            # Keyed by the raw (un-shifted) char, not `display` -- keeps
            # each button's ImGui ID stable across Shift toggles instead of
            # churning nav/hover state every time the label text changes.
            if imgui.button(f"{display}##vk_{ch}"):
                session.live_text += display
            imgui.same_line()
        imgui.new_line()

    if imgui.button("Shift##vk_shift"):
        session.shift_active = not session.shift_active
    imgui.same_line()
    if imgui.button(("123" if not session.symbols_active else "ABC") + "##vk_symbols"):
        session.symbols_active = not session.symbols_active
    imgui.same_line()
    if imgui.button("Space##vk_space"):
        session.live_text += " "
    imgui.same_line()
    if imgui.button("Backspace##vk_backspace"):
        session.live_text = session.live_text[:-1]
