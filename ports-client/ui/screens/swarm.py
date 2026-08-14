"""Swarm: Overview (read-only federation list) / Tailnet (join an existing
swarm over Tailscale) / Local Network (pair with a nearby drone over LAN) /
Reference ROMs (mount a paired peer's library via network share) / Request
Assets (browse a paired peer's inventory and pull individual items or the
current filtered collection).

Tailnet and Local Network only cover the "connect this drone" action, not
ongoing peer management -- see client/endpoints.py's module comments for
what's deliberately not wrapped (rotate-auth-key/sharing/pull-from-peer for
Tailnet; forget/dismiss for Local Network) and why. Request Assets is
scoped to what peer inventory actually supports -- Systems+ROMs and Movies,
no Music (the backend has no "music" peer-inventory type at all, see
client/endpoints.py). Download state comes from the local Drone's actual
transfer queue rather than treating the initial queue request as completion.
"""

import time

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
_DOWNLOAD_PROGRESS_WIDTH = 190.0
_DOWNLOAD_POLL_SECONDS = 0.75
_DOWNLOAD_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "skipped"}

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
        item.get("relative_path")
        or item.get("rom_path")
        or item.get("file_path")
        or item.get("movie_name")
        or item.get("name")
        or ""
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
        self.ignore_existing_games = True
        self.request_downloads = {}
        self.request_batch = None
        self.request_download_error = None
        self._next_download_poll_at = 0.0
        # (asset_type, item, label, system) for a request armed this frame,
        # executed at the top of *next* frame's draw() -- not this same
        # draw() call, or the "Downloading..." spinner drawn below would
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

    def _queue_tab(self, tab: str, label: str) -> None:
        self.tab = tab
        if tab not in self._loaded_tabs:
            self.defer_action(f"Loading {label}...", lambda: self._ensure_loaded(tab))

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
            self.defer_action("Refreshing swarm...", self._reload_overview)
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
            self.defer_action("Refreshing Tailnet...", self._reload_tailnet)
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
                self.defer_action("Joining Tailnet...", self._enroll_tailnet)

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
            self.defer_action("Discovering nearby Drones...", self._discover_lan)
        imgui.same_line()
        if imgui.button("Refresh"):
            self.defer_action("Refreshing nearby Drones...", self._reload_lan)
        imgui.spacing()

        if self.lan_error:
            imgui.text_colored(ERROR_COLOR, self.lan_error)
            return

        pairing = self.lan.get("pairing") or {}
        code = pairing.get("code", "")
        imgui.text(f"This drone's pairing code: {code}")
        imgui.text_disabled("Give this code to another drone's owner so they can pair with you.")
        if imgui.button("Rotate Code"):
            self.defer_action("Rotating pairing code...", self._rotate_pairing_code)

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
            self.defer_action(
                f"Pairing with {name}...",
                lambda selected_peer=peer_id, selected_name=name: self._pair_with(
                    selected_peer, selected_name
                ),
            )
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
            self.defer_action("Refreshing ROM references...", self._reload_reference)
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
                self.defer_action(
                    f"Unreferencing {name}...",
                    lambda selected_peer=peer_id, selected_name=name: self._disable_reference(
                        selected_peer, selected_name
                    ),
                )
        else:
            if imgui.button(f"Reference##{peer_id}"):
                self.defer_action(
                    f"Referencing {name}...",
                    lambda selected_peer=peer_id, selected_name=name: self._enable_reference(
                        selected_peer, selected_name
                    ),
                )

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
        self.request_downloads = {}
        self.request_batch = None
        self._reload_request_summary()

    def _queue_request_peer(self, peer_id: str, name: str) -> None:
        self.request_peer_id = peer_id
        self.request_peer_name = name
        self.request_kind = _REQUEST_KIND_SYSTEMS
        self.request_selected_system = None
        self.request_roms = []
        self.request_roms_query = ""
        self.request_movies = []
        self.request_movies_query = ""
        self.request_message = None
        self.request_downloads = {}
        self.request_batch = None
        self.defer_action(f"Loading assets from {name}...", self._reload_request_summary)

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

    def _queue_request_kind(self, kind: str, label: str) -> None:
        self.request_kind = kind
        if kind == _REQUEST_KIND_MOVIES and not self.request_movies and not self.request_movies_error:
            self.defer_action(f"Loading {label.lower()}...", self._reload_request_movies)

    def _select_request_system(self, system: str) -> None:
        self.request_selected_system = system
        self.request_roms_query = ""
        self._reload_request_roms()

    def _queue_request_system(self, system: str) -> None:
        self.request_selected_system = system
        self.request_roms_query = ""
        self.defer_action(f"Loading {system} games...", self._reload_request_roms)

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
            result = endpoints.request_asset(
                self.api_client,
                self.request_peer_id,
                asset_type,
                item,
                system=system,
                ignore_existing=self.ignore_existing_games,
            )
            jobs = result.get("jobs", []) if isinstance(result, dict) else []
            if not jobs and isinstance(result, dict) and isinstance(result.get("job"), dict):
                jobs = [result["job"]]
            key = (asset_type, _request_item_key(item))
            if jobs:
                self._track_download(key, label, jobs)
                self.request_message = f"Download queued: {label}."
            elif isinstance(result, dict) and ("jobs" in result or result.get("rom_skipped")):
                self.request_downloads[key] = {
                    "label": label,
                    "job_ids": [],
                    "jobs": {},
                    "status": "completed",
                    "percentage": 100.0,
                    "message": "Already downloaded",
                }
                self._mark_inventory_item_downloaded(key)
                self.request_message = f"Already downloaded: {label}."
            else:
                # Compatibility with older Drone releases that returned only
                # {status: queued}; there is no job UUID to follow, but the
                # request was still accepted successfully.
                self.request_message = f"Download started: {label}."
            self._next_download_poll_at = 0.0
        except DroneApiError as error:
            self.request_message = str(error)

    def _download_all(self) -> None:
        asset_type = "roms" if self.request_kind == _REQUEST_KIND_SYSTEMS else "movies"
        system = self.request_selected_system if asset_type == "roms" else ""
        query = self.request_roms_query if asset_type == "roms" else self.request_movies_query
        if asset_type == "roms" and not system:
            self.request_message = "Select a system before downloading all games."
            return
        try:
            before = endpoints.downloads(self.api_client)
            known_ids = {
                str(job.get("job_id") or job.get("id") or "")
                for job in (before.get("downloads") or [])
                if isinstance(job, dict)
            }
            result = endpoints.request_assets_bulk(
                self.api_client,
                self.request_peer_id,
                asset_type,
                system=system or "",
                query=query,
                ignore_existing=self.ignore_existing_games,
            )
            after = endpoints.downloads(self.api_client)
            queued_job_ids = [
                str(value)
                for value in (result.get("queued_job_ids") or [])
                if str(value)
            ] if isinstance(result, dict) else []
            after_by_id = {
                str(job.get("job_id") or job.get("id") or ""): job
                for job in (after.get("downloads") or [])
                if isinstance(job, dict) and (job.get("job_id") or job.get("id"))
            }
            queued_job_rows = result.get("queued_jobs") or [] if isinstance(result, dict) else []
            if queued_job_rows:
                new_jobs = []
                for initial in queued_job_rows:
                    if not isinstance(initial, dict):
                        continue
                    job_id = str(initial.get("job_id") or initial.get("id") or "")
                    current = after_by_id.get(job_id)
                    # A job missing from the first post-bulk snapshot already
                    # completed and aged out of the queue's bounded recent
                    # list while the server was still enumerating the batch.
                    new_jobs.append(
                        {**initial, **current}
                        if current
                        else {**initial, "status": "completed", "percentage": 100.0}
                    )
            else:
                new_jobs = [
                    job
                    for job in (after.get("downloads") or [])
                    if isinstance(job, dict)
                    and (
                        str(job.get("job_id") or job.get("id") or "") in queued_job_ids
                        if queued_job_ids
                        else str(job.get("job_id") or job.get("id") or "") not in known_ids
                    )
                    and str(job.get("source_drone_id") or "") == str(self.request_peer_id or "")
                    and self._job_asset_type(job) == asset_type
                ]
            primary_jobs = [job for job in new_jobs if str(job.get("file_type") or "").upper() != "ARTWORK"]
            discovered_ids = []
            for job in primary_jobs:
                item_key = self._job_item_key(job)
                job_id = str(job.get("job_id") or job.get("id") or "")
                if not item_key or not job_id:
                    continue
                discovered_ids.append(job_id)
                self._track_download(
                    (asset_type, item_key),
                    str(job.get("file_name") or item_key),
                    [job],
                )
            batch_ids = queued_job_ids or discovered_ids
            completed_before_first_poll = [job_id for job_id in batch_ids if job_id not in discovered_ids]
            skipped = int(result.get("skipped_existing") or 0) if isinstance(result, dict) else 0
            queued = int(result.get("queued_assets") or len(primary_jobs)) if isinstance(result, dict) else len(primary_jobs)
            self.request_batch = {
                "asset_type": asset_type,
                "job_ids": batch_ids,
                "completed_missing_ids": completed_before_first_poll,
                "queued": queued,
                "skipped": skipped,
                "status": "completed" if not batch_ids else "queued",
            }
            noun = "game" if asset_type == "roms" else "movie"
            self.request_message = f"Queued {queued} {noun} download(s); ignored {skipped} existing."
            if not batch_ids and skipped:
                self._mark_visible_existing_items_downloaded()
            self._next_download_poll_at = 0.0
        except DroneApiError as error:
            self.request_message = str(error)

    @staticmethod
    def _job_asset_type(job: dict) -> str:
        explicit = str(job.get("asset_type") or "").lower()
        if explicit in {"roms", "movies"}:
            return explicit
        file_type = str(job.get("file_type") or "").lower()
        return "movies" if file_type == "movie" else "roms" if file_type == "rom" else explicit

    @staticmethod
    def _job_item_key(job: dict) -> str:
        return str(
            job.get("relative_path")
            or job.get("file_path")
            or job.get("rom_name")
            or job.get("movie_name")
            or job.get("file_name")
            or ""
        )

    def _track_download(self, key, label: str, jobs) -> None:
        job_map = {
            str(job.get("job_id") or job.get("id") or ""): dict(job)
            for job in jobs
            if isinstance(job, dict) and (job.get("job_id") or job.get("id"))
        }
        first = next(iter(job_map.values()), {})
        status = str(first.get("status") or "queued").lower()
        percentage = self._job_percentage(first)
        if status in {"completed", "skipped"}:
            percentage = 100.0
            message = "Downloaded" if status == "completed" else "Already downloaded"
        elif status == "downloading":
            message = f"{percentage:.0f}%"
        else:
            message = status.title() if status else "Queued"
        self.request_downloads[key] = {
            "label": label,
            "job_ids": list(job_map),
            "jobs": job_map,
            "status": status,
            "percentage": percentage,
            "message": message,
        }
        if status in {"completed", "skipped"}:
            self._mark_inventory_item_downloaded(key)

    def _refresh_download_progress(self) -> None:
        try:
            self._apply_download_snapshot(endpoints.downloads(self.api_client))
            self.request_download_error = None
        except DroneApiError as error:
            self.request_download_error = str(error)
        finally:
            self._next_download_poll_at = time.monotonic() + _DOWNLOAD_POLL_SECONDS

    def _apply_download_snapshot(self, snapshot: dict) -> None:
        current_jobs = {
            str(job.get("job_id") or job.get("id") or ""): job
            for job in (snapshot.get("downloads") or [])
            if isinstance(job, dict) and (job.get("job_id") or job.get("id"))
        }
        for key, tracked in self.request_downloads.items():
            for job_id in tracked.get("job_ids") or []:
                if job_id in current_jobs:
                    tracked["jobs"][job_id] = dict(current_jobs[job_id])
            jobs = list((tracked.get("jobs") or {}).values())
            if not jobs:
                continue
            primary = jobs[0]
            status = str(primary.get("status") or "queued").lower()
            tracked["status"] = status
            tracked["percentage"] = self._job_percentage(primary)
            if status == "queued":
                position = primary.get("queue_position")
                tracked["message"] = f"Queued #{position}" if position else "Queued"
            elif status == "downloading":
                tracked["message"] = f"{tracked['percentage']:.0f}%"
            elif status in {"completed", "skipped"}:
                tracked["percentage"] = 100.0
                tracked["message"] = "Downloaded" if status == "completed" else "Already downloaded"
                self._mark_inventory_item_downloaded(key)
            elif status in {"failed", "cancelled"}:
                tracked["message"] = str(
                    primary.get("error_message") or primary.get("failure_reason") or status.title()
                )

        if self.request_batch:
            states = [self._job_from_tracking(job_id) for job_id in self.request_batch.get("job_ids") or []]
            states = [job for job in states if job]
            missing_done = len(self.request_batch.get("completed_missing_ids") or [])
            expected = len(self.request_batch.get("job_ids") or [])
            if expected and len(states) + missing_done >= expected and all(
                str(job.get("status") or "") in _DOWNLOAD_TERMINAL_STATUSES for job in states
            ):
                failed = any(str(job.get("status") or "") in {"failed", "cancelled"} for job in states)
                self.request_batch["status"] = "failed" if failed else "completed"
            elif states:
                self.request_batch["status"] = "downloading"

    def _job_from_tracking(self, job_id: str):
        for tracked in self.request_downloads.values():
            job = (tracked.get("jobs") or {}).get(job_id)
            if job:
                return job
        return None

    @staticmethod
    def _job_percentage(job: dict) -> float:
        try:
            reported = float(job.get("percentage") or 0)
        except (TypeError, ValueError):
            reported = 0.0
        if reported > 0:
            return min(100.0, max(0.0, reported))
        try:
            downloaded = float(job.get("downloaded_bytes") or job.get("bytes_transferred") or 0)
            total = float(job.get("total_bytes") or job.get("file_size") or 0)
        except (TypeError, ValueError):
            return 0.0
        return min(100.0, max(0.0, downloaded * 100.0 / total)) if total > 0 else 0.0

    def _mark_inventory_item_downloaded(self, key) -> None:
        asset_type, item_key = key
        rows = self.request_roms if asset_type == "roms" else self.request_movies
        for row in rows:
            if _request_item_key(row) == item_key:
                row["exists_locally"] = True
                row["_downloaded"] = True

    def _mark_visible_existing_items_downloaded(self) -> None:
        for row in self.request_roms:
            if row.get("exists_locally"):
                row["_downloaded"] = True

    def _has_active_downloads(self) -> bool:
        return any(
            str(state.get("status") or "") not in _DOWNLOAD_TERMINAL_STATUSES
            for state in self.request_downloads.values()
        )

    def _maybe_poll_downloads(self) -> None:
        if self._has_active_downloads() and time.monotonic() >= self._next_download_poll_at:
            self._refresh_download_progress()

    def _draw_download_controls(self) -> None:
        _changed, self.ignore_existing_games = imgui.checkbox(
            "Ignore existing games", self.ignore_existing_games
        )
        imgui.same_line()
        has_scope = self.request_kind == _REQUEST_KIND_MOVIES or self.request_selected_system is not None
        can_download_all = has_scope and not self._has_active_downloads()
        imgui.begin_disabled(not can_download_all)
        if imgui.button("Download All"):
            label = (
                f"Preparing all {self.request_selected_system} downloads..."
                if self.request_kind == _REQUEST_KIND_SYSTEMS
                else "Preparing all movie downloads..."
            )
            self.defer_action(label, self._download_all)
        imgui.end_disabled()
        imgui.spacing()
        if self.request_batch:
            job_ids = self.request_batch.get("job_ids") or []
            if not job_ids:
                skipped = int(self.request_batch.get("skipped") or 0)
                message = (
                    f"No new downloads — ignored {skipped} existing asset(s)."
                    if skipped
                    else "No matching assets were available to download."
                )
                imgui.text_colored(SUCCESS_COLOR, message)
                return
            jobs = [self._job_from_tracking(job_id) for job_id in job_ids]
            jobs = [job for job in jobs if job]
            total = max(1, len(job_ids))
            missing_done = len(self.request_batch.get("completed_missing_ids") or [])
            progress = (
                missing_done
                + sum(
                    1.0
                    if str(job.get("status") or "") in _DOWNLOAD_TERMINAL_STATUSES
                    else self._job_percentage(job) / 100.0
                    for job in jobs
                )
            ) / total
            terminal = missing_done + sum(
                1 for job in jobs if str(job.get("status") or "") in _DOWNLOAD_TERMINAL_STATUSES
            )
            overlay = f"{terminal}/{len(job_ids)} complete"
            width = min(520.0, imgui.get_content_region_avail().x)
            imgui.progress_bar(progress, imgui.ImVec2(width, 0), overlay)
        if self.request_download_error:
            imgui.text_colored(WARNING_COLOR, f"Download status unavailable: {self.request_download_error}")

    def _draw_download_state(self, asset_type: str, item: dict) -> bool:
        key = (asset_type, _request_item_key(item))
        tracked = self.request_downloads.get(key)
        if tracked:
            status = str(tracked.get("status") or "")
            if status in {"completed", "skipped"}:
                imgui.text_colored(SUCCESS_COLOR, tracked.get("message") or "Downloaded")
            elif status in {"failed", "cancelled"}:
                imgui.text_colored(ERROR_COLOR, tracked.get("message") or status.title())
            else:
                fraction = float(tracked.get("percentage") or 0.0) / 100.0
                imgui.progress_bar(
                    fraction,
                    imgui.ImVec2(_DOWNLOAD_PROGRESS_WIDTH, 0),
                    tracked.get("message") or status.title(),
                )
            return True
        if item.get("exists_locally") or item.get("_downloaded"):
            imgui.text_colored(SUCCESS_COLOR, "Downloaded")
            return True
        return False

    def _draw_request(self) -> None:
        self._maybe_poll_downloads()
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
            tab_button(
                label,
                active=self.request_kind == key,
                on_click=lambda k=key, value=label: self._queue_request_kind(k, value),
            )
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
            self.defer_action("Refreshing peer list...", self._reload_request_peers)
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
                self._queue_request_peer(peer_id, name)
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
        self._draw_download_controls()
        if self.request_selected_system is None:
            imgui.text_disabled("Select a system to browse its games.")
        else:
            self.request_roms_query, triggered = search_box("request_roms_search", self.request_roms_query)
            if triggered:
                self.defer_action("Searching games...", self._reload_request_roms)

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
            self._queue_request_system(clicked_system)

    def _draw_request_rom_row(self, index: int, rom: dict) -> None:
        name = rom.get("name") or rom.get("rom_file") or ""
        imgui.text(name)
        imgui.same_line()
        if self._draw_download_state("roms", rom):
            if (rom.get("exists_locally") or rom.get("_downloaded")) and not self.ignore_existing_games:
                imgui.same_line()
                if imgui.button(f"Download Again##request_rom_{index}"):
                    self._request_item("roms", rom, name, system=self.request_selected_system)
                    imgui.same_line()
                    widgets.spinner()
                    imgui.same_line()
                    imgui.text_disabled("Starting...")
        elif self._pending_request_key == ("roms", _request_item_key(rom)):
            widgets.spinner()
            imgui.same_line()
            imgui.text_disabled("Downloading...")
        elif imgui.button(f"Download##request_rom_{index}"):
            self._request_item("roms", rom, name, system=self.request_selected_system)
            imgui.same_line()
            widgets.spinner()
            imgui.same_line()
            imgui.text_disabled("Starting...")

    def _draw_request_movies(self) -> None:
        self._draw_download_controls()
        self.request_movies_query, triggered = search_box("request_movies_search", self.request_movies_query)
        if triggered:
            self.defer_action("Searching movies...", self._reload_request_movies)

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
            if self._draw_download_state("movies", movie):
                can_download_again = (
                    movie.get("exists_locally") or movie.get("_downloaded")
                ) and not self.ignore_existing_games
                if can_download_again:
                    imgui.same_line()
                    if imgui.button(f"Download Again##request_movie_{index}"):
                        self._request_item("movies", movie, label)
                        imgui.same_line()
                        widgets.spinner()
                        imgui.same_line()
                        imgui.text_disabled("Starting...")
            elif self._pending_request_key == ("movies", _request_item_key(movie)):
                widgets.spinner()
                imgui.same_line()
                imgui.text_disabled("Downloading...")
            elif imgui.button(f"Download##request_movie_{index}"):
                self._request_item("movies", movie, label)
                imgui.same_line()
                widgets.spinner()
                imgui.same_line()
                imgui.text_disabled("Starting...")
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
            tab_button(label, active=self.tab == key, on_click=lambda k=key, value=label: self._queue_tab(k, value))
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
