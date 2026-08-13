"""Reproduces the Drone web UI's actual color palette (app/web/static/css/drone.css
``:root``) instead of a generic ImGui dark theme, per the "look and feel like the
web UI" requirement -- same dark navy surfaces, same cyan accent for active/hover
states, same near-white text.
"""

from imgui_bundle import imgui

# Straight from drone.css's :root custom properties.
ADMIN_BG = "#101828"
ADMIN_SURFACE = "#151f32"
ADMIN_SURFACE_MUTED = "#1f2a44"
ADMIN_BORDER = "#31405f"
ADMIN_SIDEBAR = "#0b1020"
ACCENT = "#00c2ff"
ACCENT_GREEN = "#34d399"
ACCENT_COIN = "#ffbf3f"
TEXT = "#ecf6ff"
MUTED = "#9fb0c9"


def hex_color(hex_value: str, alpha: float = 1.0) -> "imgui.ImVec4":
    hex_value = hex_value.lstrip("#")
    r, g, b = (int(hex_value[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    return imgui.ImVec4(r, g, b, alpha)


_color = hex_color  # short internal alias used throughout apply_drone_theme below


def apply_drone_theme() -> None:
    style = imgui.get_style()
    Col = imgui.Col_

    style.window_rounding = 6.0
    style.frame_rounding = 4.0
    style.grab_rounding = 4.0
    style.tab_rounding = 4.0
    style.window_border_size = 1.0
    style.frame_border_size = 1.0

    style.set_color_(Col.window_bg.value, _color(ADMIN_BG))
    style.set_color_(Col.child_bg.value, _color(ADMIN_SURFACE))
    style.set_color_(Col.popup_bg.value, _color(ADMIN_SURFACE))
    style.set_color_(Col.border.value, _color(ADMIN_BORDER))
    style.set_color_(Col.text.value, _color(TEXT))
    style.set_color_(Col.text_disabled.value, _color(MUTED))

    style.set_color_(Col.frame_bg.value, _color(ADMIN_SURFACE_MUTED))
    style.set_color_(Col.frame_bg_hovered.value, _color(ACCENT, 0.18))
    style.set_color_(Col.frame_bg_active.value, _color(ACCENT, 0.28))

    style.set_color_(Col.button.value, _color(ADMIN_SURFACE_MUTED))
    style.set_color_(Col.button_hovered.value, _color(ACCENT, 0.18))
    style.set_color_(Col.button_active.value, _color(ACCENT, 0.32))

    style.set_color_(Col.header.value, _color(ACCENT, 0.12))
    style.set_color_(Col.header_hovered.value, _color(ACCENT, 0.20))
    style.set_color_(Col.header_active.value, _color(ACCENT, 0.30))

    style.set_color_(Col.tab.value, _color(ADMIN_SURFACE))
    style.set_color_(Col.tab_hovered.value, _color(ACCENT, 0.24))
    style.set_color_(Col.tab_selected.value, _color(ACCENT, 0.32))
    style.set_color_(Col.tab_dimmed.value, _color(ADMIN_SURFACE))
    style.set_color_(Col.tab_dimmed_selected.value, _color(ADMIN_SURFACE_MUTED))

    style.set_color_(Col.title_bg.value, _color(ADMIN_SIDEBAR))
    style.set_color_(Col.title_bg_active.value, _color(ADMIN_SIDEBAR))
    style.set_color_(Col.menu_bar_bg.value, _color(ADMIN_SIDEBAR))

    style.set_color_(Col.check_mark.value, _color(ACCENT))
    style.set_color_(Col.slider_grab.value, _color(ACCENT))
    style.set_color_(Col.slider_grab_active.value, _color(ACCENT))

    style.set_color_(Col.scrollbar_bg.value, _color(ADMIN_BG))
    style.set_color_(Col.scrollbar_grab.value, _color(ADMIN_BORDER))
    style.set_color_(Col.scrollbar_grab_hovered.value, _color(ACCENT, 0.4))
    style.set_color_(Col.scrollbar_grab_active.value, _color(ACCENT, 0.6))

    style.set_color_(Col.separator.value, _color(ADMIN_BORDER))
    style.set_color_(Col.text_selected_bg.value, _color(ACCENT, 0.35))


# Status colors shared by every screen that reports an API error or an
# online/offline state (LoginScreen, SwarmScreen, VpnScreen, ...) -- named
# here once instead of re-inlining the same ImVec4 in each screen.
ERROR_COLOR = hex_color("#f06060")
SUCCESS_COLOR = hex_color(ACCENT_GREEN)
MUTED_COLOR = hex_color(MUTED)
WARNING_COLOR = hex_color(ACCENT_COIN)
