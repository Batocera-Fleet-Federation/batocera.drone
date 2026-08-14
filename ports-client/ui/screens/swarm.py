"""Swarm: Overview (read-only federation list) / Tailnet (join an existing
swarm over Tailscale) / Local Network (pair with a nearby drone over LAN) /
Reference ROMs (mount a paired peer's library via network share) / Request
Assets (browse a paired peer's inventory and pull individual items).

Tailnet and Local Network only cover the "connect this drone" action, not
ongoing peer management -- see client/endpoints.py's module comments for
what's deliberately not wrapped (rotate-auth-key/sharing/pull-from-peer for
Tailnet; forget/dismiss for Local Network) and why. Request Assets is
scoped to what peer inventory actually supports -- Systems+ROMs and Movies,
no Music (the backend has no "music" peer-inventory type at all, see
client/endpoints.py) -- and single-item requests, not "sync everything".
"""

from imgui_bundle import imgui

from client import endpoints
from client.errors import DroneApiError

from .. import virtual_keyboard, widgets
from ..theme import ERROR_COLOR, SUCCESS_COLOR, WARNING_COLOR
from ..widgets import search_box, tab_button
from .base import Screen

_LIST_HEIGHT = 260.0
_STATUS_COLUMN_X = 340.0
_SYSTEMS_PANE_WIDTH = 280.0

_TAB_OVERVIEW = "overview"
_TAB_TAILNET = "tailnet"
_TAB_LAN = "lan"
_TAB_REFERENCE = "reference"
_TAB_REQUEST = "request"
_TABS = (
    (_TAB_OVERVIEW, "Overview"),
    (_TAB_TAILNET, "Tailnet"),
    (_TAB_LAN, "Local Network"),
    (_TAB_REFERENCE, "Reference ROMs"),
    (_TAB_REQUEST, "Request Assets"),
)

_REQUEST_KIND_SYSTEMS = "systems"
_REQUEST_KIND_MOVIES = "movies"
_REQUEST_KINDS = ((_REQUEST_KIND_SYSTEMS, "Systems"), (_REQUEST_KIND_MOVIES, "Movies"))


def _request_item_key(item: dict) -> str:
    """A stable-enough identifier for matching a rom/movie dict back to a
    pending request across frames -- same fields _enqueue_local_asset itself
    treats as identity server-side, just used here for UI matching only."""
    return str(
        item.get("relative_path") or item.get("rom_path") or item.get("file_path") or item.get("name") or ""
    )


class SwarmScreen(Screen):
    def __init__(self, api_client):
        self.api_client = api_client
        self.tab = _TAB_OVERVIEW
        self._loaded_tabs = set()

        # Overview
        self.active = False
        self.drones = []
        self.overview_error = None

        # Tailnet
        self.tailnet = {}
        self.tailnet_error = None
        self.tailnet_auth_key = ""
        self.tailnet_message = None

        # Local Network
        self.lan = {}
        self.lan_error = None
        self.lan_message = None
        self.pairing_code_inputs = {}

        # Reference ROMs
        self.reference_peers = []  # list of (peer_dict, share_dict_or_None)
        self.reference_error = None
        self.reference_message = None

        # Request Assets
        self.request_peers = []
        self.request_peers_error = None
        self.request_peer_id = None
        self.request_peer_name = ""
        self.request_kind = _REQUEST_KIND_SYSTEMS
        self.request_summary = {}
        self.request_summary_error = None
        self.request_selected_system = None
        self.request_roms = []
        self.request_roms_total = 0
        self.request_roms_error = None
        self.request_roms_query = ""
        self.request_movies = []
        self.request_movies_error = None
        self.request_movies_query = ""
        self.request_message = None
        # (asset_type, item, label, system) for a request armed this frame,
        # executed at the top of *next* frame's draw() -- not this same
        # draw() call, or the "Requesting..." spinner drawn below would
        # never actually get presented before the blocking POST freezes the
        # UI (see backups.py's _apply_pending_id for the full reasoning).
        self._pending_request = None
        self._pending_request_key = None

    def on_enter(self) -> None:
        self._ensure_loaded(self.tab)

    def _ensure_loaded(self, tab: str) -> None:
        if tab in self._loaded_tabs:
            return
        self._loaded_tabs.add(tab)
        if tab == _TAB_OVERVIEW:
            self._reload_overview()
        elif tab == _TAB_TAILNET:
            self._reload_tailnet()
        elif tab == _TAB_LAN:
            self._reload_lan()
        elif tab == _TAB_REFERENCE:
            self._reload_reference()
        else:
            self._reload_request_peers()

    def _select_tab(self, tab: str) -> None:
        self.tab = tab
        self._ensure_loaded(tab)

    # --- Overview ------------------------------------------------------

    def _reload_overview(self) -> None:
        try:
            result = endpoints.swarm_overview(self.api_client)
            self.active = bool(result.get("active")) if isinstance(result, dict) else False
            self.drones = result.get("drones", []) if isinstance(result, dict) else []
            self.overview_error = None
        except DroneApiError as error:
            self.overview_error = str(error)

    def _draw_overview(self) -> None:
        if imgui.button("Refresh"):
            self._reload_overview()
        imgui.spacing()

        if self.overview_error:
            imgui.text_colored(ERROR_COLOR, self.overview_error)
            return

        if not self.active:
            imgui.text_disabled("Local Network / Swarm mode is not active on this drone.")
            return

        imgui.begin_child("drones_list", imgui.ImVec2(0, _LIST_HEIGHT), True)
        if not self.drones:
            imgui.text_disabled("No paired drones yet.")
        for drone in self.drones:
            self._draw_drone_row(drone)
        imgui.end_child()

    @staticmethod
    def _draw_drone_row(drone: dict) -> None:
        name = drone.get("name") or drone.get("hostname") or drone.get("drone_id") or "unknown"
        if drone.get("is_self"):
            name = f"{name} (this drone)"
        imgui.text(name)

        imgui.same_line(_STATUS_COLUMN_X)
        if drone.get("online"):
            imgui.text_colored(SUCCESS_COLOR, "Online")
        else:
            imgui.text_colored(ERROR_COLOR, "Offline")

        error = drone.get("error")
        if error:
            imgui.same_line()
            imgui.text_disabled(f"-- {error}")

        summary = drone.get("summary") or {}
        counts = summary.get("counts")
        if isinstance(counts, dict) and counts:
            parts = [f"{key}: {value}" for key, value in counts.items()]
            imgui.text_disabled("   " + ", ".join(parts))

        imgui.separator()

    # --- Tailnet ---------------------------------------------------------

    def _reload_tailnet(self) -> None:
        try:
            self.tailnet = endpoints.tailnet_status(self.api_client)
            self.tailnet_error = None
        except DroneApiError as error:
            self.tailnet_error = str(error)

    def _enroll_tailnet(self) -> None:
        try:
            result = endpoints.tailnet_enroll(self.api_client, self.tailnet_auth_key)
            self.tailnet_message = f"Result: {result.get('status')}"
            self.tailnet_auth_key = ""
        except DroneApiError as error:
            self.tailnet_message = str(error)
        self._reload_tailnet()

    def _draw_tailnet(self) -> None:
        if imgui.button("Refresh"):
            self._reload_tailnet()
        imgui.spacing()

        if self.tailnet_error:
            imgui.text_colored(ERROR_COLOR, self.tailnet_error)
            return

        if not self.tailnet.get("installed"):
            imgui.text_colored(ERROR_COLOR, "Tailscale is not installed on this drone.")
            imgui.spacing()
            # Matches drone.js's Swarm-page Tailnet card wording -- the
            # installer vendors the static tailscale/tailscaled binaries
            # under /userdata/system/tailscale, so a missing binary means
            # that step never ran (or ran against an old Drone build from
            # before this existed), not that Tailscale needs configuring
            # here. There's no keyboard on a console to type the reinstall
            # command with, so this just tells the owner what to do from
            # another device rather than offering to run it in-app.
            imgui.text_wrapped(
                "Re-run the Drone installer once from another device (it now sets "
                "the mesh up automatically), then come back here to connect:"
            )
            imgui.spacing()
            imgui.text_colored(
                WARNING_COLOR,
                "curl -fsSL https://github.com/Batocera-Fleet-Federation/"
                "batocera.drone/releases/latest/download/batocera_install.sh | bash",
            )
            return

        if self.tailnet.get("enrolled"):
            imgui.text_colored(SUCCESS_COLOR, "Enrolled")
            imgui.spacing()
            for label, key in (
                ("Tailnet", "tailnet_name"),
                ("DNS name", "dns_name"),
                ("Tailnet IP", "tailnet_ip"),
                ("Relay", "relay"),
                ("Version", "version"),
            ):
                value = self.tailnet.get(key)
                if value:
                    imgui.text(f"{label}: {value}")
            for line in self.tailnet.get("health") or []:
                imgui.text_colored(WARNING_COLOR, str(line))
        else:
            imgui.text_colored(WARNING_COLOR, "Not enrolled")
            imgui.spacing()
            imgui.text_wrapped(
                "Paste an auth key from your Tailscale admin console to join this "
                "drone to an existing tailnet."
            )
            imgui.set_next_item_width(400)
            _, self.tailnet_auth_key = virtual_keyboard.input_text(
                "Auth key", self.tailnet_auth_key, imgui.InputTextFlags_.password.value
            )
            if imgui.button("Enroll") and self.tailnet_auth_key.strip():
                self._enroll_tailnet()

        if self.tailnet_message:
            imgui.spacing()
            imgui.text_disabled(self.tailnet_message)

    # --- Local Network -----------------------------------------------------

    def _reload_lan(self) -> None:
        try:
            self.lan = endpoints.local_network_status(self.api_client)
            self.lan_error = None
        except DroneApiError as error:
            self.lan_error = str(error)

    def _discover_lan(self) -> None:
        try:
            self.lan = endpoints.local_network_discover(self.api_client)
            self.lan_error = None
        except DroneApiError as error:
            self.lan_error = str(error)

    def _rotate_pairing_code(self) -> None:
        try:
            result = endpoints.local_network_rotate_pairing_code(self.api_client)
            self.lan["pairing"] = result.get("pairing")
        except DroneApiError as error:
            self.lan_message = str(error)

    def _pair_with(self, peer_id: str, peer_label: str) -> None:
        code = self.pairing_code_inputs.get(peer_id, "")
        try:
            endpoints.local_network_pair(self.api_client, peer_id, code)
            self.lan_message = f"Paired with {peer_label}."
            self.pairing_code_inputs.pop(peer_id, None)
        except DroneApiError as error:
            self.lan_message = str(error)
        self._reload_lan()

    def _draw_lan(self) -> None:
        if imgui.button("Discover Nearby Drones"):
            self._discover_lan()
        imgui.same_line()
        if imgui.button("Refresh"):
            self._reload_lan()
        imgui.spacing()

        if self.lan_error:
            imgui.text_colored(ERROR_COLOR, self.lan_error)
            return

        pairing = self.lan.get("pairing") or {}
        code = pairing.get("code", "")
        imgui.text(f"This drone's pairing code: {code}")
        imgui.text_disabled("Give this code to another drone's owner so they can pair with you.")
        if imgui.button("Rotate Code"):
            self._rotate_pairing_code()

        imgui.spacing()
        imgui.separator()
        imgui.spacing()
        imgui.text("Nearby drones")

        peers = self.lan.get("peers") or []
        if not peers:
            imgui.text_disabled("None discovered yet -- click Discover Nearby Drones.")
        for peer in peers:
            self._draw_discovered_peer(peer)

        if self.lan_message:
            imgui.spacing()
            imgui.text_disabled(self.lan_message)

    def _draw_discovered_peer(self, peer: dict) -> None:
        peer_id = str(peer.get("drone_id") or "")
        name = peer.get("name") or peer.get("hostname") or peer_id
        imgui.text(name)

        current_code = self.pairing_code_inputs.get(peer_id, "")
        imgui.set_next_item_width(150)
        _, current_code = virtual_keyboard.input_text(f"Their code##{peer_id}", current_code)
        self.pairing_code_inputs[peer_id] = current_code

        imgui.same_line()
        if imgui.button(f"Pair##{peer_id}") and current_code.strip():
            self._pair_with(peer_id, name)
        imgui.separator()

    # --- Reference ROMs ------------------------------------------------

    def _reload_reference(self) -> None:
        try:
            overview = endpoints.swarm_overview(self.api_client)
            peers = [row for row in (overview.get("drones") or []) if not row.get("is_self")]
            shares_result = endpoints.network_shares(self.api_client)
            shares = shares_result.get("shares", []) if isinstance(shares_result, dict) else []
            shares_by_peer = {str(row.get("peer_id")): row for row in shares}
            self.reference_peers = [(peer, shares_by_peer.get(str(peer.get("drone_id")))) for peer in peers]
            self.reference_error = None
        except DroneApiError as error:
            self.reference_error = str(error)

    def _enable_reference(self, peer_id: str, name: str) -> None:
        try:
            endpoints.network_share_enable(self.api_client, peer_id)
            self.reference_message = f"Referencing {name} -- this can take a moment."
        except DroneApiError as error:
            self.reference_message = str(error)
        self._reload_reference()

    def _disable_reference(self, peer_id: str, name: str) -> None:
        try:
            endpoints.network_share_disable(self.api_client, peer_id)
            self.reference_message = f"Unreferencing {name}."
        except DroneApiError as error:
            self.reference_message = str(error)
        self._reload_reference()

    def _draw_reference(self) -> None:
        if imgui.button("Refresh"):
            self._reload_reference()
        imgui.spacing()

        if self.reference_error:
            imgui.text_colored(ERROR_COLOR, self.reference_error)
            return

        imgui.text_wrapped(
            "Reference a paired drone's ROM and BIOS library over the network "
            "instead of copying it -- games appear here without using local storage."
        )
        imgui.spacing()

        if not self.reference_peers:
            imgui.text_disabled("No paired drones yet.")
        for peer, share in self.reference_peers:
            self._draw_reference_row(peer, share)

        if self.reference_message:
            imgui.spacing()
            imgui.text_disabled(self.reference_message)

    def _draw_reference_row(self, peer: dict, share: dict) -> None:
        peer_id = str(peer.get("drone_id") or "")
        name = peer.get("name") or peer_id
        imgui.text(name)

        imgui.same_line(_STATUS_COLUMN_X)
        enabled = bool(share.get("enabled")) if share else False
        status = str(share.get("status") or "") if share else ""
        if enabled and status == "mounted":
            imgui.text_colored(SUCCESS_COLOR, "Referenced")
        elif enabled:
            imgui.text_colored(WARNING_COLOR, status or "pending")
        else:
            imgui.text_disabled("Not referenced")

        imgui.same_line()
        if enabled:
            if imgui.button(f"Unreference##{peer_id}"):
                self._disable_reference(peer_id, name)
        else:
            if imgui.button(f"Reference##{peer_id}"):
                self._enable_reference(peer_id, name)

        if share and share.get("status_detail"):
            imgui.text_disabled(f"   {share['status_detail']}")
        imgui.separator()

    # --- Request Assets --------------------------------------------------

    def _reload_request_peers(self) -> None:
        try:
            overview = endpoints.swarm_overview(self.api_client)
            self.request_peers = [row for row in (overview.get("drones") or []) if not row.get("is_self")]
            self.request_peers_error = None
        except DroneApiError as error:
            self.request_peers_error = str(error)

    def _select_request_peer(self, peer_id: str, name: str) -> None:
        self.request_peer_id = peer_id
        self.request_peer_name = name
        self.request_kind = _REQUEST_KIND_SYSTEMS
        self.request_selected_system = None
        self.request_roms = []
        self.request_roms_query = ""
        self.request_movies = []
        self.request_movies_query = ""
        self.request_message = None
        self._reload_request_summary()

    def _leave_request_peer(self) -> None:
        self.request_peer_id = None

    def _reload_request_summary(self) -> None:
        try:
            result = endpoints.peer_asset_summary(self.api_client, self.request_peer_id)
            self.request_summary = result.get("system_counts", {}) if isinstance(result, dict) else {}
            self.request_summary_error = None
        except DroneApiError as error:
            self.request_summary_error = str(error)

    def _select_request_kind(self, kind: str) -> None:
        self.request_kind = kind
        if kind == _REQUEST_KIND_MOVIES and not self.request_movies and not self.request_movies_error:
            self._reload_request_movies()

    def _select_request_system(self, system: str) -> None:
        self.request_selected_system = system
        self.request_roms_query = ""
        self._reload_request_roms()

    def _reload_request_roms(self) -> None:
        try:
            result = endpoints.peer_roms(
                self.api_client, self.request_peer_id, self.request_selected_system,
                limit=200, query=self.request_roms_query,
            )
            self.request_roms = result.get("items", []) if isinstance(result, dict) else []
            self.request_roms_total = result.get("total", len(self.request_roms)) if isinstance(result, dict) else len(self.request_roms)
            self.request_roms_error = None
        except DroneApiError as error:
            self.request_roms_error = str(error)
            self.request_roms = []

    def _reload_request_movies(self) -> None:
        try:
            result = endpoints.peer_movies(
                self.api_client, self.request_peer_id, limit=200, query=self.request_movies_query
            )
            self.request_movies = result.get("items", []) if isinstance(result, dict) else []
            self.request_movies_error = None
        except DroneApiError as error:
            self.request_movies_error = str(error)

    def _request_item(self, asset_type: str, item: dict, label: str, *, system: str = "") -> None:
        self._pending_request = (asset_type, item, label, system)
        self._pending_request_key = (asset_type, _request_item_key(item))
        self.request_message = None

    def _do_request_item(self, asset_type: str, item: dict, label: str, system: str) -> None:
        try:
            endpoints.request_asset(self.api_client, self.request_peer_id, asset_type, item, system=system)
            self.request_message = f"Requested {label}."
        except DroneApiError as error:
            self.request_message = str(error)

    def _draw_request(self) -> None:
        if self.request_peer_id is None:
            self._draw_request_peer_picker()
            return

        if imgui.button("Back to peer list"):
            self._leave_request_peer()
            return
        imgui.same_line()
        imgui.text_disabled(f"Browsing: {self.request_peer_name}")
        imgui.spacing()

        for key, label in _REQUEST_KINDS:
            tab_button(label, active=self.request_kind == key, on_click=lambda k=key: self._select_request_kind(k))
            imgui.same_line()
        imgui.new_line()
        imgui.spacing()

        if self.request_kind == _REQUEST_KIND_SYSTEMS:
            self._draw_request_systems()
        else:
            self._draw_request_movies()

        if self.request_message:
            imgui.spacing()
            imgui.text_disabled(self.request_message)

    def _draw_request_peer_picker(self) -> None:
        if imgui.button("Refresh"):
            self._reload_request_peers()
        imgui.spacing()

        if self.request_peers_error:
            imgui.text_colored(ERROR_COLOR, self.request_peers_error)
            return

        if not self.request_peers:
            imgui.text_disabled("No paired drones yet.")
        for peer in self.request_peers:
            peer_id = str(peer.get("drone_id") or "")
            name = peer.get("name") or peer_id
            if imgui.button(f"Browse##{peer_id}"):
                self._select_request_peer(peer_id, name)
            imgui.same_line()
            imgui.text(name)
            imgui.separator()

    def _draw_request_systems(self) -> None:
        if self.request_summary_error:
            imgui.text_colored(ERROR_COLOR, self.request_summary_error)
            return

        clicked_system = None
        imgui.begin_child("request_systems_list", imgui.ImVec2(_SYSTEMS_PANE_WIDTH, _LIST_HEIGHT), True)
        for name, count in sorted(self.request_summary.items()):
            label = f"{name} ({count})"
            _, selected = imgui.selectable(label, self.request_selected_system == name)
            if selected:
                clicked_system = name
        imgui.end_child()

        imgui.same_line()
        imgui.begin_group()
        if self.request_selected_system is None:
            imgui.text_disabled("Select a system to browse its games.")
        else:
            self.request_roms_query, triggered = search_box("request_roms_search", self.request_roms_query)
            if triggered:
                self._reload_request_roms()

            if self.request_roms_error:
                imgui.text_colored(ERROR_COLOR, self.request_roms_error)
            else:
                imgui.text(f"{self.request_selected_system} -- {self.request_roms_total} games")
                imgui.begin_child("request_roms_list", imgui.ImVec2(0, _LIST_HEIGHT - 40), True)
                for index, rom in enumerate(self.request_roms):
                    self._draw_request_rom_row(index, rom)
                imgui.end_child()
        imgui.end_group()

        if clicked_system is not None and clicked_system != self.request_selected_system:
            self._select_request_system(clicked_system)

    def _draw_request_rom_row(self, index: int, rom: dict) -> None:
        name = rom.get("name") or rom.get("rom_file") or ""
        imgui.text(name)
        imgui.same_line()
        if self._pending_request_key == ("roms", _request_item_key(rom)):
            widgets.spinner()
            imgui.same_line()
            imgui.text_disabled("Requesting...")
        elif rom.get("exists_locally"):
            imgui.text_disabled("(already have)")
        elif imgui.button(f"Request##request_rom_{index}"):
            self._request_item("roms", rom, name, system=self.request_selected_system)

    def _draw_request_movies(self) -> None:
        self.request_movies_query, triggered = search_box("request_movies_search", self.request_movies_query)
        if triggered:
            self._reload_request_movies()

        if self.request_movies_error:
            imgui.text_colored(ERROR_COLOR, self.request_movies_error)
            return

        imgui.begin_child("request_movies_list", imgui.ImVec2(0, _LIST_HEIGHT), True)
        if not self.request_movies:
            imgui.text_disabled("Nothing here yet.")
        for index, movie in enumerate(self.request_movies):
            label = movie.get("display_title") or movie.get("movie_name") or ""
            imgui.text(label)
            imgui.same_line()
            if self._pending_request_key == ("movies", _request_item_key(movie)):
                widgets.spinner()
                imgui.same_line()
                imgui.text_disabled("Requesting...")
            elif imgui.button(f"Request##request_movie_{index}"):
                self._request_item("movies", movie, label)
        imgui.end_child()

    # --- draw ------------------------------------------------------------

    def draw(self, navigator) -> None:
        # Must run before anything below can arm a new pending request this
        # same frame -- see _pending_request's comment in __init__.
        if self._pending_request is not None:
            asset_type, item, label, system = self._pending_request
            self._pending_request = None
            self._pending_request_key = None
            self._do_request_item(asset_type, item, label, system)

        for key, label in _TABS:
            tab_button(label, active=self.tab == key, on_click=lambda k=key: self._select_tab(k))
            imgui.same_line()
        imgui.new_line()
        imgui.spacing()

        if self.tab == _TAB_OVERVIEW:
            self._draw_overview()
        elif self.tab == _TAB_TAILNET:
            self._draw_tailnet()
        elif self.tab == _TAB_LAN:
            self._draw_lan()
        elif self.tab == _TAB_REFERENCE:
            self._draw_reference()
        else:
            self._draw_request()
