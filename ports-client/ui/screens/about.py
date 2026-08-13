"""About: the landing tab -- what this app is, why it exists specifically as
a Ports-menu companion (not just "the same thing as the browser"), and where
to find the full web dashboard for everything this console app deliberately
doesn't cover. Mirrors drone.js's own landing page (``renderHelpPage``, the
``#home``/``#help`` route) in tone, condensed for a gamepad-navigable screen
with no icon font / rich HTML available.
"""

from typing import Optional

from imgui_bundle import imgui

from client import endpoints
from client.errors import DroneApiError

from .. import assets
from ..theme import ACCENT, ERROR_COLOR, MUTED_COLOR, hex_color
from .base import Screen

_LOGO_DISPLAY_WIDTH = 320.0
# ui/assets/logo.png is a 946x946 canvas built for EmulationStation's wide
# marquee slot -- the wordmark itself only occupies a thin horizontal band
# (rows 309-617, measured via PIL's alpha-channel bbox), with large
# transparent margins above/below that make sense in a wide marquee frame but
# just look like a gap here. Crop to that band via UV coords instead of
# displaying the full transparent square.
_LOGO_CONTENT_UV0 = imgui.ImVec2(0.0, 0.327)
_LOGO_CONTENT_UV1 = imgui.ImVec2(1.0, 0.652)

_BODY_PARAGRAPHS = (
    "This is the Ports-menu companion to the Batocera Drone service already "
    "running on this machine -- the same swarm, VPN, and backup tools "
    "available from the browser, rebuilt so every one of them is fully "
    "navigable with a gamepad, right here in EmulationStation. No "
    "keyboard, mouse, or second device required.",
    "Drone itself runs quietly in the background on this machine and turns "
    "it into a management dashboard reachable from any phone, tablet, or "
    "computer on your network. Pair machines together on the Swarm page and "
    "they act as one fleet -- encrypted peer-to-peer transfers, live health, "
    "and remote access, with no central server and no port forwarding.",
)

_SCOPE_NOTE = (
    "This console app covers Swarm, VPN, and Backups -- the parts that make "
    "sense with a controller in hand. For everything else (library browsing "
    "and artwork, Torrents, System Info, Automation, notifications) open the "
    "full dashboard in a browser at:"
)


class AboutScreen(Screen):
    def __init__(self, api_client):
        self.api_client = api_client
        self.web_url = ""
        self.error: Optional[str] = None

    def on_enter(self) -> None:
        self._reload_url()

    def _reload_url(self) -> None:
        try:
            result = endpoints.swarm_overview(self.api_client)
            drones = result.get("drones") if isinstance(result, dict) else None
            self_row = next((row for row in (drones or []) if row.get("is_self")), None)
            self.web_url = str((self_row or {}).get("reachable_url") or "")
            self.error = None
        except DroneApiError as error:
            self.error = str(error)

    def draw(self, navigator) -> None:
        self._draw_logo()
        imgui.text_colored(hex_color(ACCENT), "Batocera Fleet Federation")
        imgui.text_wrapped("Run your whole collection like a fleet -- not one machine at a time.")
        imgui.spacing()
        imgui.separator()
        imgui.spacing()

        for paragraph in _BODY_PARAGRAPHS:
            imgui.text_wrapped(paragraph)
            imgui.spacing()

        imgui.separator()
        imgui.spacing()
        imgui.text_wrapped(_SCOPE_NOTE)
        imgui.spacing()
        if self.web_url:
            imgui.text_colored(hex_color(ACCENT), self.web_url)
        elif self.error:
            imgui.text_colored(ERROR_COLOR, self.error)
        else:
            imgui.text_colored(MUTED_COLOR, "(loading...)")

    def _draw_logo(self) -> None:
        # assets.logo_texture() lazily uploads a GPU texture on first call --
        # deliberately only ever reached from draw(), never on_enter(): a
        # texture upload needs a live GL context, which only exists once the
        # real render loop is running (ui/app.py's run()), and on_enter() is
        # also reachable from plain unit tests with no window/context at all,
        # where this call doesn't raise a catchable Python exception but
        # hard-crashes the process.
        loaded = assets.logo_texture()
        if loaded is None:
            return
        texture_id, size = loaded
        width, height = size.x, size.y
        if width <= 0:
            return
        content_height = height * (_LOGO_CONTENT_UV1.y - _LOGO_CONTENT_UV0.y)
        scale = _LOGO_DISPLAY_WIDTH / width
        imgui.image(
            imgui.ImTextureRef(texture_id),
            imgui.ImVec2(width * scale, content_height * scale),
            _LOGO_CONTENT_UV0,
            _LOGO_CONTENT_UV1,
        )
        imgui.spacing()
