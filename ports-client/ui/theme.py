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
ADMIN_SIDEBAR_GRADIENT_START = "#111936"  # drone.css .sidebar: linear-gradient(90deg, #111936 0%, var(--admin-sidebar) 100%)
ACCENT = "#00c2ff"
ACCENT_HOT = "#ff3ea5"
ACCENT_GREEN = "#34d399"
ACCENT_COIN = "#ffbf3f"
TEXT = "#ecf6ff"
MUTED = "#9fb0c9"
# body's 42px repeating 1px-line grid, drawn faint over the window background
# (drone.css's own `linear-gradient(rgba(255,255,255,0.035) 1px, transparent
# 1px)`, twice, one per axis) -- same alpha, same spacing.
_GRID_SPACING = 42.0
_GRID_LINE_ALPHA = 0.035


def hex_color(hex_value: str, alpha: float = 1.0) -> "imgui.ImVec4":
    hex_value = hex_value.lstrip("#")
    r, g, b = (int(hex_value[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    return imgui.ImVec4(r, g, b, alpha)


_color = hex_color  # short internal alias used throughout apply_drone_theme below


def apply_drone_theme() -> None:
    style = imgui.get_style()
    Col = imgui.Col_

    style.window_rounding = 8.0
    style.frame_rounding = 6.0
    style.grab_rounding = 6.0
    style.tab_rounding = 6.0
    style.popup_rounding = 8.0
    style.child_rounding = 8.0
    style.window_border_size = 1.0
    style.frame_border_size = 1.0
    # A little roomier than ImGui's cramped defaults -- closer to Bootstrap's
    # own card/button padding, part of what makes the web UI not feel flat.
    style.window_padding = imgui.ImVec2(14.0, 14.0)
    style.frame_padding = imgui.ImVec2(10.0, 6.0)
    style.item_spacing = imgui.ImVec2(10.0, 8.0)

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


def draw_background_grid(io: "imgui.IO") -> None:
    """Call once per frame, before drawing any screen content. Reproduces
    drone.css's ``body`` background (a solid fill plus its own two faint
    42px ``linear-gradient(...1px, transparent 1px)`` grid layers) on
    ImGui's background draw list -- this was the single biggest "looks
    plain" gap versus the web UI, since ImGui has no page background/
    texture concept of its own. Only visible where ``ui/app.py``'s root
    window uses ``WindowFlags_.no_background`` -- otherwise the root
    window's own opaque fill paints over this entirely, the same way a
    solid-background page element would hide the body behind it."""
    draw_list = imgui.get_background_draw_list()
    width, height = io.display_size.x, io.display_size.y
    draw_list.add_rect_filled(imgui.ImVec2(0.0, 0.0), imgui.ImVec2(width, height), imgui.get_color_u32(_color(ADMIN_BG)))

    line_color = imgui.get_color_u32(_color(TEXT, _GRID_LINE_ALPHA))
    x = 0.0
    while x <= width:
        draw_list.add_line(imgui.ImVec2(x, 0.0), imgui.ImVec2(x, height), line_color)
        x += _GRID_SPACING

    y = 0.0
    while y <= height:
        draw_list.add_line(imgui.ImVec2(0.0, y), imgui.ImVec2(width, y), line_color)
        y += _GRID_SPACING


# Status colors shared by every screen that reports an API error or an
# online/offline state (LoginScreen, SwarmScreen, VpnScreen, ...) -- named
# here once instead of re-inlining the same ImVec4 in each screen.
ERROR_COLOR = hex_color("#f06060")
SUCCESS_COLOR = hex_color(ACCENT_GREEN)
MUTED_COLOR = hex_color(MUTED)
WARNING_COLOR = hex_color(ACCENT_COIN)
