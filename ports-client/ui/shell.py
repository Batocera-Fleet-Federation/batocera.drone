"""Persistent top bar + content pane, drawn every frame once logged in.

Deliberately mirrors drone.js's own flow rather than a stack of full-screen
menus with "Back" buttons: an always-visible nav bar highlights the active
section and switches the content pane in place.

Scoped to swarm-related activities only (see ports-client/README.md): About
(the landing tab -- what this app is and a pointer to the full web UI for
everything it doesn't cover), the swarm, connect to it, reference a peer's
ROMs, download ROMs/Movies from a peer (all under Swarm's own tabs -- see
swarm.py), VPN, and Backups. Local Assets browsing and the Debug/Automation
admin tiles were removed. VPN and Backups are flat top-level sections rather
than nested under an "Admin" grouping -- with Debug and Automation gone, an
Admin wrapper around just two items was pure overhead.
"""

from imgui_bundle import imgui

from client.http_client import DroneApiClient

from . import assets
from .screens.about import AboutScreen
from .screens.backups import BackupsScreen
from .screens.base import Screen
from .screens.swarm import SwarmScreen
from .screens.vpn import VpnScreen
from .theme import ACCENT_HOT, ADMIN_SIDEBAR, ADMIN_SIDEBAR_GRADIENT_START, hex_color
from .widgets import loading_panel, tab_button

_SECTIONS = (("about", "About"), ("swarm", "Swarm"), ("vpn", "VPN"), ("backups", "Backups"))
_QUIT_AREA_WIDTH = 90.0
_TOP_BAR_HEIGHT = 44.0
_LOGO_ICON_HEIGHT = 26.0


class AppShell(Screen):
    def __init__(self, api_client: DroneApiClient, username: str, on_quit):
        self.api_client = api_client
        self.username = username
        self.on_quit = on_quit
        self.section = "about"
        self._content = {
            "about": AboutScreen(api_client),
            "swarm": SwarmScreen(api_client),
            "vpn": VpnScreen(api_client),
            "backups": BackupsScreen(api_client),
        }
        self._entered_keys = set()

    def _ensure_entered(self, key: str) -> None:
        if key not in self._entered_keys:
            self._content[key].on_enter()
            self._entered_keys.add(key)

    def on_enter(self) -> None:
        self._ensure_entered(self.section)

    def draw(self, navigator) -> None:
        current = self._content[self.section]
        current.run_deferred_action()
        busy = self.deferred_action_label is not None or current.deferred_action_label is not None
        imgui.begin_disabled(busy)
        self._draw_top_bar()
        # Quick tab-switch bonus -- D-pad/stick nav can already reach these
        # same top-bar buttons directly once ui/gamepad.py's HasGamepad fix
        # is live, so this is polish, not a functional gap. gamepad_l1/r1
        # only exist as ImGui keys because ui/gamepad.py's
        # handle_shoulder_button feeds them in -- the vendored SDL2 backend
        # doesn't translate shoulder buttons at all.
        if imgui.is_key_pressed(imgui.Key.gamepad_l1):
            self._cycle_section(-1)
        elif imgui.is_key_pressed(imgui.Key.gamepad_r1):
            self._cycle_section(1)
        imgui.separator()
        imgui.spacing()
        current.draw(navigator)
        imgui.end_disabled()
        if current.deferred_action_label:
            loading_panel(current.deferred_action_label)

    def _draw_top_bar(self) -> None:
        self._draw_top_bar_background()

        logo = assets.logo_texture()
        if logo is not None:
            texture_id, size = logo
            scale = _LOGO_ICON_HEIGHT / size.y if size.y > 0 else 0.0
            imgui.image(imgui.ImTextureRef(texture_id), imgui.ImVec2(size.x * scale, size.y * scale))
            imgui.same_line()

        imgui.text_colored(hex_color(ACCENT_HOT), "Batocera Drone")
        imgui.same_line()
        imgui.text_disabled(f"-- {self.username}")
        imgui.same_line(0, 40)

        for key, label in _SECTIONS:
            tab_button(label, active=self.section == key, on_click=lambda k=key, value=label: self._queue_section(k, value))
            imgui.same_line()

        imgui.same_line(max(0.0, imgui.get_window_width() - _QUIT_AREA_WIDTH))
        if imgui.button("Quit"):
            self.on_quit()

    @staticmethod
    def _draw_top_bar_background() -> None:
        # drone.css's .sidebar: linear-gradient(90deg, #111936 0%, var(--admin-sidebar)
        # 100%) -- reproduced left-to-right across the top bar's own row so the
        # shell doesn't sit on the same flat window_bg as everything else.
        draw_list = imgui.get_window_draw_list()
        top_left = imgui.get_cursor_screen_pos()
        width = imgui.get_window_width()
        bottom_right = imgui.ImVec2(top_left.x + width, top_left.y + _TOP_BAR_HEIGHT)
        start_color = imgui.get_color_u32(hex_color(ADMIN_SIDEBAR_GRADIENT_START))
        end_color = imgui.get_color_u32(hex_color(ADMIN_SIDEBAR))
        draw_list.add_rect_filled_multi_color(top_left, bottom_right, start_color, end_color, end_color, start_color)

    def _select_section(self, section: str) -> None:
        self.section = section
        self._ensure_entered(section)

    def _queue_section(self, section: str, label: str) -> None:
        if section in self._entered_keys:
            self.section = section
            return
        if self.defer_action(f"Loading {label}...", lambda: self._ensure_entered(section)):
            self.section = section

    def _cycle_section(self, direction: int) -> None:
        keys = [key for key, _label in _SECTIONS]
        next_index = (keys.index(self.section) + direction) % len(keys)
        next_key = keys[next_index]
        next_label = dict(_SECTIONS)[next_key]
        self._queue_section(next_key, next_label)
