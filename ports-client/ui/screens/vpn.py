"""Admin -> VPN: status + Connect/Disconnect only.

Uploading a .ovpn file, typing credentials, the peer-sharing toggle, and
pull-from-peer are deliberately out of scope here -- they're form-heavy
admin actions (a file picker, typed text) with no practical gamepad-only
UI on a console. Use the browser UI for those; once a config + credentials
exist (uploaded there, or shared from a paired peer), this screen can drive
the connection day-to-day without a second device.
"""

from imgui_bundle import imgui

from client import endpoints
from client.errors import DroneApiError

from ..theme import ERROR_COLOR, SUCCESS_COLOR, WARNING_COLOR
from .base import Screen


class VpnScreen(Screen):
    def __init__(self, api_client):
        self.api_client = api_client
        self.status = {}
        self.error = None
        self.action_message = None

    def on_enter(self) -> None:
        self._reload()

    def _reload(self) -> None:
        try:
            self.status = endpoints.vpn_status(self.api_client)
            self.error = None
        except DroneApiError as error:
            self.error = str(error)

    def _connect(self) -> None:
        try:
            result = endpoints.vpn_connect(self.api_client)
            self.action_message = f"Result: {result.get('status')}"
        except DroneApiError as error:
            self.action_message = str(error)
        self._reload()

    def _disconnect(self) -> None:
        try:
            result = endpoints.vpn_disconnect(self.api_client)
            self.action_message = f"Result: {result.get('status')}"
        except DroneApiError as error:
            self.action_message = str(error)
        self._reload()

    def draw(self, navigator) -> None:
        if imgui.button("Refresh"):
            self._reload()
        imgui.spacing()

        if self.error:
            imgui.text_colored(ERROR_COLOR, self.error)
            return

        status_value = self.status.get("status", "unknown")
        if status_value == "connected":
            imgui.text_colored(SUCCESS_COLOR, f"Status: {status_value}")
        elif status_value == "error":
            imgui.text_colored(ERROR_COLOR, f"Status: {status_value}")
        else:
            imgui.text_colored(WARNING_COLOR, f"Status: {status_value}")

        if self.status.get("message"):
            imgui.text_disabled(self.status["message"])

        if not self.status.get("installed"):
            imgui.spacing()
            imgui.text_colored(ERROR_COLOR, "openvpn is not installed on this drone.")
            return

        if not self.status.get("has_config"):
            imgui.spacing()
            imgui.text_disabled("No VPN config uploaded yet -- upload one from the browser UI's VPN tile.")
            return

        imgui.spacing()
        remotes = self.status.get("remotes") or []
        if remotes:
            imgui.text(f"Server: {', '.join(remotes)}")
        if self.status.get("username"):
            imgui.text(f"Username: {self.status['username']}")
        if self.status.get("tunnel_ip"):
            imgui.text(f"Tunnel IP: {self.status['tunnel_ip']}")
        duration = self.status.get("connected_duration_seconds")
        if duration is not None:
            imgui.text(f"Connected for: {_format_duration(duration)}")
        if self.status.get("source_peer_name"):
            imgui.text_disabled(f"Shared from: {self.status['source_peer_name']}")
        if self.status.get("revoked_reason"):
            imgui.text_colored(ERROR_COLOR, f"Revoked: {self.status['revoked_reason']}")

        imgui.spacing()
        if status_value == "connected":
            if imgui.button("Disconnect"):
                self._disconnect()
        else:
            if imgui.button("Connect"):
                self._connect()

        if self.action_message:
            imgui.spacing()
            imgui.text_disabled(self.action_message)


def _format_duration(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"
