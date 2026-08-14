"""Small widgets shared across screens: the active/inactive tab button used
by AppShell's flat top nav (Swarm/VPN/Backups) and SwarmScreen's own internal
tab row (Overview/Tailnet/Local Network/Reference ROMs/Request Assets), the
search box used by Request Assets -- queries a peer's assets the same way
the drone does -- and a loading spinner for the handful of blocking calls
that can run long enough to be worth an explicit "still working" signal.
"""

import math

from imgui_bundle import imgui

from . import virtual_keyboard
from .theme import ACCENT, hex_color

_ACTIVE_BUTTON_COLOR = imgui.ImVec4(0, 194 / 255, 1.0, 0.32)
_ACTIVE_TEXT_COLOR = hex_color(ACCENT)
_SEARCH_WIDTH = 260.0
_SPINNER_COLOR = hex_color(ACCENT)
_SPINNER_SEGMENTS = 24


def tab_button(label: str, *, active: bool, on_click) -> None:
    if active:
        imgui.push_style_color(imgui.Col_.button.value, _ACTIVE_BUTTON_COLOR)
        imgui.push_style_color(imgui.Col_.text.value, _ACTIVE_TEXT_COLOR)
    if imgui.button(label) and not active:
        on_click()
    if active:
        imgui.pop_style_color(2)


def search_box(widget_id: str, current_query: str):
    """Text field + Search/Clear buttons. Always returns the live typed text
    (feed it back into your own state every frame, same as any other
    input_text) plus whether a search should actually fire this frame
    (Enter, Search click, or Clear) -- so the displayed text stays in sync
    with typing without firing a request on every keystroke."""
    imgui.set_next_item_width(_SEARCH_WIDTH)
    enter_pressed, new_value = virtual_keyboard.input_text(
        f"##{widget_id}", current_query, imgui.InputTextFlags_.enter_returns_true.value
    )
    imgui.same_line()
    search_clicked = imgui.button(f"Search##{widget_id}_btn")
    imgui.same_line()
    clear_clicked = imgui.button(f"Clear##{widget_id}_clear")
    imgui.spacing()
    if clear_clicked:
        return "", True
    return new_value, (enter_pressed or search_clicked)


def spinner(radius: float = 7.0, thickness: float = 3.0) -> None:
    """A small rotating arc, same-line-able with a "Working..." label.
    Nothing in ports-client is threaded, so this is purely a "the app hasn't
    frozen, a real blocking call is in flight" signal, not real progress --
    screens that use it pair it with the two-phase deferred-call pattern
    (see backups.py/swarm.py) so this actually gets drawn and presented on a
    real frame before the blocking call that follows it.
    """
    draw_list = imgui.get_window_draw_list()
    top_left = imgui.get_cursor_screen_pos()
    center = imgui.ImVec2(top_left.x + radius, top_left.y + radius)
    imgui.dummy(imgui.ImVec2(radius * 2, radius * 2))

    time = imgui.get_time()
    start_angle = time * 8.0
    sweep = math.pi * 1.5
    points = [
        imgui.ImVec2(
            center.x + math.cos(start_angle + sweep * i / (_SPINNER_SEGMENTS - 1)) * radius,
            center.y + math.sin(start_angle + sweep * i / (_SPINNER_SEGMENTS - 1)) * radius,
        )
        for i in range(_SPINNER_SEGMENTS)
    ]
    draw_list.add_polyline(points, imgui.get_color_u32(_SPINNER_COLOR), thickness, 0)
