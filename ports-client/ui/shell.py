"""Persistent top bar + content pane, drawn every frame once logged in.

Deliberately mirrors drone.js's own flow rather than a stack of full-screen
menus with "Back" buttons: an always-visible nav bar highlights the active
section and switches the content pane in place.

Scoped to swarm-related activities only (see ports-client/README.md): show
the swarm, connect to it, reference a peer's ROMs, download ROMs/Movies from
a peer (all under Swarm's own tabs -- see swarm.py), VPN, and Backups. Local
Assets browsing and the Debug/Automation admin tiles were removed. VPN and
Backups are flat top-level sections rather than nested under an "Admin"
grouping -- with Debug and Automation gone, an Admin wrapper around just
two items was pure overhead.
"""

from imgui_bundle import imgui

from client.http_client import DroneApiClient

from .screens.backups import BackupsScreen
from .screens.base import Screen
from .screens.swarm import SwarmScreen
from .screens.vpn import VpnScreen
from .widgets import tab_button

_SECTIONS = (("swarm", "Swarm"), ("vpn", "VPN"), ("backups", "Backups"))
_QUIT_AREA_WIDTH = 90.0


class AppShell(Screen):
    def __init__(self, api_client: DroneApiClient, username: str, on_quit):
        self.api_client = api_client
        self.username = username
        self.on_quit = on_quit
        self.section = "swarm"
        self._content = {
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
        self._draw_top_bar()
        imgui.separator()
        imgui.spacing()
        self._content[self.section].draw(navigator)

    def _draw_top_bar(self) -> None:
        imgui.text("Batocera Drone")
        imgui.same_line()
        imgui.text_disabled(f"-- {self.username}")
        imgui.same_line(0, 40)

        for key, label in _SECTIONS:
            tab_button(label, active=self.section == key, on_click=lambda k=key: self._select_section(k))
            imgui.same_line()

        imgui.same_line(max(0.0, imgui.get_window_width() - _QUIT_AREA_WIDTH))
        if imgui.button("Quit"):
            self.on_quit()

    def _select_section(self, section: str) -> None:
        self.section = section
        self._ensure_entered(section)
