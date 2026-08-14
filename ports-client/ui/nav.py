"""Minimal current-screen state machine driving the render loop.

Not a history stack -- screens navigate forward/back explicitly by
constructing the next Screen and calling go_to(), same pattern drone.js
uses for its own hash-based routing (see the plan's UX-parity note).
"""

from typing import Optional

from imgui_bundle import imgui

from client.http_client import DroneApiClient

from .screens.base import Screen
from .widgets import loading_panel


class Navigator:
    def __init__(self, api_client: DroneApiClient):
        self.api_client = api_client
        self._screen: Optional[Screen] = None

    def go_to(self, screen: Screen) -> None:
        self._screen = screen
        screen.on_enter()

    def draw(self) -> None:
        screen = self._screen
        if screen is not None:
            screen.run_deferred_action()
            busy = screen.deferred_action_label is not None
            imgui.begin_disabled(busy)
            screen.draw(self)
            imgui.end_disabled()
            if screen.deferred_action_label:
                loading_panel(screen.deferred_action_label)
