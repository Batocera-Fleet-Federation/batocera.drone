"""Small widgets shared across screens: the active/inactive tab button used
by AppShell's flat top nav (Swarm/VPN/Backups) and SwarmScreen's own internal
tab row (Overview/Tailnet/Local Network/Reference ROMs/Request Assets), and
the search box used by Request Assets -- queries a peer's assets the same
way the drone does.
"""

from imgui_bundle import imgui

from . import virtual_keyboard
from .theme import ACCENT, hex_color

_ACTIVE_BUTTON_COLOR = imgui.ImVec4(0, 194 / 255, 1.0, 0.32)
_ACTIVE_TEXT_COLOR = hex_color(ACCENT)
_SEARCH_WIDTH = 260.0


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
