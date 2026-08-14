"""Minimal current-screen state machine driving the render loop.

Not a history stack -- screens navigate forward/back explicitly by
constructing the next Screen and calling go_to(), same pattern drone.js
uses for its own hash-based routing (see the plan's UX-parity note).
"""

from typing import Optional

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
        if self._screen is not None:
            self._screen.run_deferred_action()
            self._screen.draw(self)
            if self._screen.deferred_action_label:
                loading_panel(self._screen.deferred_action_label)
