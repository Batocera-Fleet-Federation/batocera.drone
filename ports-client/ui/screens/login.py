"""Connecting screen, with a manual login form as a rare fallback.

The Ports client always talks to Drone over loopback (127.0.0.1) by design
(it runs on the same device), and Drone's session gate now treats a
loopback caller as pre-authenticated with no cookie needed -- physical
access to the device is already the codebase's root of trust, so this app
was never meant to make someone type a password to talk to the box they're
already sitting at. See app/common/auth.py's SessionAuth.authenticate_request
docstring for the server-side half of this.

The manual username/password form below only ever shows up if that
loopback trust doesn't apply for some reason -- DRONE_PORTS_CLIENT_HOST
pointed at a non-loopback host for dev/testing, or a Drone build old enough
to not have the loopback exemption yet.
"""

from imgui_bundle import imgui

from client.errors import AuthenticationError, DroneApiError

from ..theme import ERROR_COLOR
from .base import Screen


class LoginScreen(Screen):
    def __init__(self, api_client, on_authenticated):
        self.api_client = api_client
        self.on_authenticated = on_authenticated
        self.username = ""
        self.password = ""
        self.error = None
        self.connection_error = None
        self.connecting = False

    def on_enter(self) -> None:
        self.error = None
        self.connection_error = None
        self.connecting = True
        try:
            status = self.api_client.session_status()
            if status.get("authenticated"):
                self.on_authenticated(status.get("username", ""))
                return
        except DroneApiError as error:
            self.connection_error = str(error)
        finally:
            self.connecting = False

    def draw(self, navigator) -> None:
        imgui.text("Batocera Drone")
        imgui.spacing()

        if self.connecting:
            imgui.text("Connecting...")
            return

        if self.connection_error:
            imgui.text_colored(ERROR_COLOR, f"Could not reach Drone: {self.connection_error}")
            imgui.spacing()
            if imgui.button("Retry"):
                self.on_enter()
            return

        # Reachable, but genuinely not authenticated -- only expected when
        # DRONE_PORTS_CLIENT_HOST points somewhere other than loopback.
        imgui.set_next_item_width(320)
        _, self.username = imgui.input_text("Username", self.username)
        imgui.set_next_item_width(320)
        _, self.password = imgui.input_text(
            "Password", self.password, imgui.InputTextFlags_.password.value
        )

        if self.error:
            imgui.spacing()
            imgui.text_colored(ERROR_COLOR, self.error)

        imgui.spacing()
        if imgui.button("Log in") and self.username and self.password:
            self._attempt_login()

    def _attempt_login(self) -> None:
        try:
            username = self.api_client.login(self.username, self.password)
        except AuthenticationError:
            self.error = "Invalid username or password."
            self.password = ""
        except DroneApiError as error:
            self.error = str(error)
        else:
            self.error = None
            self.on_authenticated(username)
