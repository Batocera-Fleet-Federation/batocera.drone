"""Admin -> VPN: status, Connect/Disconnect, credentials, sharing, and
getting a config onto the device.

Uploading a .ovpn file via a file-picker dialog and pull-from-peer's own
underlying peer-sharing toggle live server-side already; ports-client
offers two gamepad-native ways to get a config here instead of a file
picker: pull one from an already-configured paired peer (zero typing, see
_draw_pull_from_peer), or import one a PC dropped into a local folder over
Batocera's own default SMB share (see _draw_import_from_folder and the new
backend endpoints in app/device/vpn_manager.py). Credentials use the
virtual keyboard (ui/virtual_keyboard.py) now that typed secrets are
gamepad-enterable -- this reverses an earlier decision to scope all of
this out as "no practical gamepad-only UI".
"""

from imgui_bundle import imgui

from client import endpoints
from client.errors import DroneApiError

from .. import virtual_keyboard
from ..theme import ERROR_COLOR, SUCCESS_COLOR, WARNING_COLOR
from .base import Screen


class VpnScreen(Screen):
    def __init__(self, api_client):
        self.api_client = api_client
        self.status = {}
        self.error = None
        self.action_message = None

        self.username_input = ""
        self.password_input = ""
        self.credentials_message = None

        self.sharing_message = None

        self.get_config_mode = None  # None | "peer" | "folder"
        self._get_config_loaded = set()
        self.pull_peers = []
        self.pull_peers_error = None
        self.pull_message = None
        self.import_files = []
        self.import_files_error = None
        self.import_directory = ""
        self.import_message = None

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

    # --- Credentials -------------------------------------------------------

    def _save_credentials(self) -> None:
        try:
            result = endpoints.vpn_credentials(self.api_client, self.username_input, self.password_input)
            self.credentials_message = f"Saved credentials for {result.get('username')}."
        except DroneApiError as error:
            self.credentials_message = str(error)
        # Never keep typed plaintext around longer than needed, success or not.
        self.password_input = ""
        self._reload()

    # --- Sharing -------------------------------------------------------------
    # The single-hop-only rule (an imported config can never be re-shared)
    # is enforced server-side too (see the drone-vpn-management skill), but
    # the guard belongs here as well, not just hidden behind draw()'s
    # conditional -- so it's testable at the state level without a live
    # imgui frame, and so this screen can never even attempt the call.

    def _set_sharing(self, enabled: bool) -> None:
        if self.status.get("source_peer_id"):
            return
        try:
            endpoints.vpn_sharing(self.api_client, enabled)
            self.sharing_message = f"VPN sharing {'enabled' if enabled else 'disabled'}."
        except DroneApiError as error:
            self.sharing_message = str(error)
        self._reload()

    # --- Get a config: pull from a paired peer --------------------------------

    def _reload_pull_peers(self) -> None:
        try:
            overview = endpoints.swarm_overview(self.api_client)
            # Matches drone.js's own loadVpnPullPeerOptions filter exactly
            # (online-only, not just paired) -- pulling from an offline
            # peer can only ever fail.
            self.pull_peers = [
                row for row in (overview.get("drones") or []) if not row.get("is_self") and row.get("online")
            ]
            self.pull_peers_error = None
        except DroneApiError as error:
            self.pull_peers_error = str(error)

    def _pull_from_peer(self, peer_id: str, name: str) -> None:
        try:
            result = endpoints.vpn_pull_from_peer(self.api_client, peer_id)
            if result.get("credentials_imported"):
                self.pull_message = f"Pulled config and credentials from {name}."
            else:
                self.pull_message = f"Pulled config from {name} (no credentials were shared)."
        except DroneApiError as error:
            self.pull_message = str(error)
        self._reload()

    # --- Get a config: import from the local drop folder ----------------------

    def _reload_import_files(self) -> None:
        try:
            result = endpoints.vpn_list_import_files(self.api_client)
            self.import_files = result.get("files", []) if isinstance(result, dict) else []
            self.import_directory = result.get("directory", "") if isinstance(result, dict) else ""
            self.import_files_error = None
        except DroneApiError as error:
            self.import_files_error = str(error)

    def _import_from_folder(self, filename: str) -> None:
        try:
            result = endpoints.vpn_import_from_folder(self.api_client, filename)
            self.import_message = f"Imported {result.get('config_filename', filename)}."
        except DroneApiError as error:
            self.import_message = str(error)
        self._reload()

    def _select_get_config_mode(self, mode: str) -> None:
        self.get_config_mode = mode
        if mode not in self._get_config_loaded:
            self._get_config_loaded.add(mode)
            if mode == "peer":
                self._reload_pull_peers()
            elif mode == "folder":
                self._reload_import_files()

    # --- Draw ----------------------------------------------------------------

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
            self._draw_get_config()
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
        if self.status.get("revoked_reason"):
            imgui.text_colored(ERROR_COLOR, f"Revoked: {self.status['revoked_reason']}")

        imgui.spacing()
        imgui.separator()
        self._draw_credentials()

        imgui.spacing()
        imgui.separator()
        self._draw_sharing()

        imgui.spacing()
        imgui.separator()
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

    def _draw_get_config(self) -> None:
        imgui.text_disabled("No VPN config uploaded yet.")
        imgui.spacing()
        if imgui.button("Pull from a Paired Drone"):
            self._select_get_config_mode("peer")
        imgui.same_line()
        if imgui.button("Import from Drop Folder"):
            self._select_get_config_mode("folder")

        imgui.spacing()
        if self.get_config_mode == "peer":
            self._draw_pull_from_peer()
        elif self.get_config_mode == "folder":
            self._draw_import_from_folder()

    def _draw_pull_from_peer(self) -> None:
        if imgui.button("Refresh##pull_peers"):
            self._reload_pull_peers()
        imgui.spacing()

        if self.pull_peers_error:
            imgui.text_colored(ERROR_COLOR, self.pull_peers_error)
            return

        if not self.pull_peers:
            imgui.text_disabled("No paired drones online.")
        for peer in self.pull_peers:
            peer_id = str(peer.get("drone_id") or "")
            name = peer.get("name") or peer.get("hostname") or peer_id
            if imgui.button(f"Pull##{peer_id}"):
                self._pull_from_peer(peer_id, name)
            imgui.same_line()
            imgui.text(name)

        if self.pull_message:
            imgui.spacing()
            imgui.text_disabled(self.pull_message)

    def _draw_import_from_folder(self) -> None:
        if imgui.button("Refresh##import_files"):
            self._reload_import_files()
        imgui.spacing()

        if self.import_directory:
            imgui.text_disabled(f"Drop a .ovpn file into: {self.import_directory}")
            imgui.text_disabled("Then Refresh.")
            imgui.spacing()

        if self.import_files_error:
            imgui.text_colored(ERROR_COLOR, self.import_files_error)
            return

        if not self.import_files:
            imgui.text_disabled("No .ovpn files found.")
        for filename in self.import_files:
            if imgui.button(f"Import##{filename}"):
                self._import_from_folder(filename)
            imgui.same_line()
            imgui.text(filename)

        if self.import_message:
            imgui.spacing()
            imgui.text_disabled(self.import_message)

    def _draw_credentials(self) -> None:
        imgui.text("Credentials")
        imgui.set_next_item_width(300)
        _, self.username_input = virtual_keyboard.input_text("VPN Username", self.username_input)
        imgui.set_next_item_width(300)
        _, self.password_input = virtual_keyboard.input_text(
            "VPN Password", self.password_input, imgui.InputTextFlags_.password.value
        )
        if imgui.button("Save Credentials"):
            self._save_credentials()
        if self.credentials_message:
            imgui.spacing()
            imgui.text_disabled(self.credentials_message)

    def _draw_sharing(self) -> None:
        source_peer_id = self.status.get("source_peer_id")
        if source_peer_id:
            source_peer_name = self.status.get("source_peer_name") or source_peer_id
            imgui.text_disabled(f"Shared from: {source_peer_name} -- cannot be re-shared")
            return
        sharing_enabled = bool(self.status.get("sharing_enabled"))
        changed, new_value = imgui.checkbox("Share with Swarm", sharing_enabled)
        if changed:
            self._set_sharing(new_value)
        if self.sharing_message:
            imgui.spacing()
            imgui.text_disabled(self.sharing_message)


def _format_duration(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"
