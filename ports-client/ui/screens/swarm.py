"""Swarm: Overview (read-only federation list) / Tailnet (join an existing
swarm over Tailscale) / Local Network (pair with a nearby drone over LAN) /
Search Assets (search and download Games across all systems, Movies & Shows,
and Music across every online paired Drone at once).

Tailnet and Local Network only cover the "connect this drone" action, not
ongoing peer management -- see client/endpoints.py's module comments for
what's deliberately not wrapped (rotate-auth-key/sharing/pull-from-peer for
Tailnet; forget/dismiss for Local Network) and why. Search Assets queries the
unified media-browse API across all online paired Drones and tracks downloads
from the local Drone's transfer queue.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from imgui_bundle import imgui

from client import endpoints
from client.errors import DroneApiError

from .. import virtual_keyboard, widgets
from ..theme import ERROR_COLOR, SUCCESS_COLOR, WARNING_COLOR
from ..widgets import button_hint_footer, search_box, tab_button
from .base import Screen

_LIST_HEIGHT = 240.0
_STATUS_COLUMN_X = 340.0
_DOWNLOAD_PROGRESS_WIDTH = 190.0
_DOWNLOAD_POLL_SECONDS = 1.0
_DOWNLOAD_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "skipped"}
_BROWSE_PAGE_LIMIT = 50
_DETAILS_POPUP_NAME = "Asset Details"

_TAB_OVERVIEW = "overview"
_TAB_TAILNET = "tailnet"
_TAB_LAN = "lan"
_TAB_SEARCH = "search"
_TABS = (
    (_TAB_OVERVIEW, "Overview"),
    (_TAB_TAILNET, "Tailnet"),
    (_TAB_LAN, "Local Network"),
    (_TAB_SEARCH, "Search Assets"),
)

_SEARCH_KIND_ROMS = "roms"
_SEARCH_KIND_MOVIES = "movies"
_SEARCH_KIND_MUSIC = "music"
_SEARCH_KINDS = (
    (_SEARCH_KIND_ROMS, "Games"),
    (_SEARCH_KIND_MOVIES, "Movies & Shows"),
    (_SEARCH_KIND_MUSIC, "Music"),
)


def _drone_status_text(drone: dict) -> str:
    """Online / Offline / Checking for one overview row.

    ``online is None`` (or ``pending``) means the row was painted from stored
    state and the per-drone probe has not finished. That must not read as
    Offline, and it must not block the rest of the page.
    """
    if drone.get("pending") or drone.get("online") is None:
        return "Checking..."
    if drone.get("online"):
        return "Online"
    return "Offline"


def _request_item_key(item: dict) -> str:
    """Stable identifier for matching a rom/movie/music dict back to a
    pending request across frames."""
    return str(
        item.get("relative_path")
        or item.get("rom_path")
        or item.get("file_path")
        or item.get("movie_name")
        or item.get("display_title")
        or item.get("name")
        or ""
    )


def _item_display_title(item: dict) -> str:
    return str(
        item.get("display_title")
        or item.get("name")
        or item.get("rom_file")
        or item.get("movie_name")
        or item.get("file_path")
        or "Unnamed asset"
    )


def _item_metadata_label(asset_kind: str, item: dict) -> str:
    if asset_kind == _SEARCH_KIND_ROMS:
        return str(item.get("system") or "")
    if asset_kind == _SEARCH_KIND_MUSIC:
        artist = str(item.get("artist") or "").strip()
        album = str(item.get("album") or "").strip()
        if artist and album:
            return f"{artist} — {album}"
        return artist or album
    show = str(item.get("show_title") or item.get("series") or "").strip()
    season = _episode_number(item.get("season"))
    episode = _episode_number(item.get("episode"))
    if show and season is not None and episode is not None:
        return f"{show} S{season:02d}E{episode:02d}"
    if show and season is not None:
        return f"{show} Season {season}"
    return show


def _episode_number(value) -> int | None:
    """A small non-negative season/episode number, or None.

    Paired peers supply this metadata, so anything that is not a plain count
    (text, floats, bools, huge values) is ignored instead of breaking rendering.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not (text.isascii() and text.isdigit() and len(text) <= 4):
            return None
        number = int(text)
    else:
        return None
    return number if 0 <= number <= 9999 else None


def _format_bytes(value) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        size = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return "0 B"


def _download_job_label(job: dict) -> str:
    """Describe what a queue job is actually transferring.

    Artwork jobs use the parent ROM name as ``file_name`` because the server
    fetches artwork by ROM identity. Without the job type, a ROM plus its image
    and marquee therefore look like three identical ZIP downloads in the UI.
    """
    subject = str(
        job.get("file_name")
        or job.get("rom_name")
        or job.get("rom_path")
        or job.get("relative_path")
        or job.get("file_path")
        or "Unnamed asset"
    )
    file_type = str(job.get("file_type") or "").strip().upper()
    if file_type == "ARTWORK":
        artwork_type = str(job.get("artwork_type") or "").strip().replace("_", " ").replace("-", " ")
        role = f"{artwork_type.title()} artwork" if artwork_type else "Artwork"
        return f"[{role}] {subject}"
    role = {
        "ROM": "Game",
        "MOVIE": "Movie",
        "MUSIC": "Music",
        "BIOS": "BIOS",
        "SAVE": "Save",
        "CONFIG_BACKUP": "Config backup",
    }.get(file_type)
    return f"[{role}] {subject}" if role else subject


class SwarmScreen(Screen):
    def __init__(self, api_client):
        self.api_client = api_client
        self.tab = _TAB_OVERVIEW
        self._loaded_tabs = set()

        # Overview
        self.active = False
        self.drones = []
        self.overview_error = None
        self._probe_generation = 0
        self._probe_thread = None

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

        # Search Assets
        self.asset_kind = _SEARCH_KIND_ROMS
        self.search_query = ""
        self.system_filter = ""
        self.drone_filter = ""
        self.hide_local_items = False
        self.ignore_existing_games = True
        self.browse_sources = []
        self.browse_source_notices = []
        self.browse_items = []
        self.available_systems = []
        self.next_page_token = ""
        self.has_more_pages = False
        self.browse_error = None
        self.request_message = None
        self.focused_row_index = 0
        self.details_item = None
        self._details_popup_just_opened = False
        self.request_peer_id = None
        self.request_downloads = {}
        self.request_batch = None
        self.request_download_error = None
        self.request_queue_snapshot = {"active": [], "queued": [], "recent": [], "downloads": []}
        self._next_download_poll_at = 0.0
        self._download_poll_thread = None
        self._download_poll_result = None
        self._download_poll_error = None
        self._pending_request_key = None

    @property
    def active_tab(self) -> str:
        return self.tab

    @active_tab.setter
    def active_tab(self, value: str) -> None:
        self.tab = value

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
        else:
            self._reload_search()

    def _select_tab(self, tab: str) -> None:
        self.tab = tab
        self._ensure_loaded(tab)

    def _queue_tab(self, tab: str, label: str) -> None:
        if tab in self._loaded_tabs:
            self.tab = tab
            return
        if self.defer_action(f"Loading {label}...", lambda: self._ensure_loaded(tab)):
            self.tab = tab

    def _step_tab(self, delta: int) -> None:
        keys = [key for key, _ in _TABS]
        labels = {key: label for key, label in _TABS}
        try:
            index = keys.index(self.tab)
        except ValueError:
            index = 0
        next_key = keys[(index + delta) % len(keys)]
        self._queue_tab(next_key, labels[next_key])

    def _step_sub_tab(self, delta: int) -> None:
        """L2/R2: on Search Assets step Games / Movies & Shows / Music; past
        either end (or on any other tab) step the Swarm sub-tabs instead, so
        every Swarm tab stays reachable from the triggers."""
        if self.tab == _TAB_SEARCH:
            kinds = [key for key, _ in _SEARCH_KINDS]
            labels = dict(_SEARCH_KINDS)
            try:
                index = kinds.index(self.asset_kind)
            except ValueError:
                index = 0
            target = index + delta
            if 0 <= target < len(kinds):
                self._queue_search_kind(kinds[target], labels[kinds[target]])
                return
        self._step_tab(delta)

    # --- Overview ------------------------------------------------------

    def _reload_overview(self) -> None:
        try:
            result = endpoints.swarm_overview(self.api_client, probe=False)
            self.active = bool(result.get("active")) if isinstance(result, dict) else False
            self.drones = result.get("drones", []) if isinstance(result, dict) else []
            self.overview_error = None
            self._start_peer_probes(self.drones)
        except DroneApiError as error:
            self.overview_error = str(error)

    def _start_peer_probes(self, drones) -> None:
        pending = [
            drone for drone in drones
            if isinstance(drone, dict) and not drone.get("is_self") and drone.get("drone_id")
        ]
        self._probe_generation += 1
        generation = self._probe_generation
        if not pending:
            self._probe_thread = None
            return
        thread = threading.Thread(
            target=self._probe_peers,
            args=(pending, generation),
            name="drone-ports-swarm-probes",
            daemon=True,
        )
        self._probe_thread = thread
        thread.start()

    def _probe_peers(self, pending, generation: int) -> None:
        workers = min(8, len(pending))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda drone: self._probe_one(drone, generation), pending))

    def _probe_one(self, drone: dict, generation: int) -> None:
        if generation != self._probe_generation:
            return
        drone_id = str(drone.get("drone_id") or "")
        try:
            payload = endpoints.swarm_probe_peer(self.api_client, drone_id)
            resolved = payload.get("drone") if isinstance(payload, dict) else None
            if not isinstance(resolved, dict) or not resolved.get("drone_id"):
                raise DroneApiError("probe failed")
        except DroneApiError as error:
            resolved = {**drone, "pending": False, "online": False, "error": str(error)}
        if generation != self._probe_generation:
            return
        resolved_id = str(resolved.get("drone_id") or drone_id)
        self.drones = [
            resolved if str(existing.get("drone_id") or "") == resolved_id else existing
            for existing in self.drones
            if isinstance(existing, dict)
        ]

    def wait_for_peer_probes(self, timeout: float = 5.0) -> None:
        thread = self._probe_thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("Timed out waiting for swarm peer probes")

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
        for index, drone in enumerate(self.drones):
            self._draw_drone_row(index, drone)
        imgui.end_child()

    @staticmethod
    def _draw_drone_row(index: int, drone: dict) -> None:
        name = drone.get("name") or drone.get("hostname") or drone.get("drone_id") or "unknown"
        if drone.get("is_self"):
            name = f"{name} (this drone)"
        imgui.selectable(f"{name}##drone_row_{index}", False)

        imgui.same_line(_STATUS_COLUMN_X)
        status = _drone_status_text(drone)
        if status == "Online":
            imgui.text_colored(SUCCESS_COLOR, f"[{status}]")
        elif status == "Checking...":
            imgui.text_disabled(f"[{status}]")
        else:
            imgui.text_colored(ERROR_COLOR, f"[{status}]")

        error = drone.get("error")
        if error:
            imgui.same_line()
            imgui.text_disabled(f"-- {error}")

        summary_error = drone.get("summary_error")
        if drone.get("online") and summary_error:
            imgui.text_colored(WARNING_COLOR, f"   Inventory delayed -- {summary_error}")

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

    # --- Search Assets --------------------------------------------------

    def _reload_search(self) -> None:
        try:
            sources_payload = endpoints.media_browse_sources(self.api_client)
            if isinstance(sources_payload, dict) and isinstance(sources_payload.get("sources"), list):
                self.browse_sources = list(sources_payload["sources"])
        except DroneApiError:
            pass
        try:
            system_arg = self.system_filter if self.asset_kind == _SEARCH_KIND_ROMS else ""
            result = endpoints.media_browse_list(
                self.api_client,
                self.asset_kind,
                q=self.search_query,
                system=system_arg,
                drones=self.drone_filter,
                exclude_local=bool(self.hide_local_items),
                limit=_BROWSE_PAGE_LIMIT,
            )
            self._apply_browse_page(result, append=False)
            self.browse_error = None
        except DroneApiError as error:
            self.browse_error = str(error)
            self.browse_items = []
            self.next_page_token = ""
            self.has_more_pages = False

    def _load_next_page(self) -> None:
        cursor = self.next_page_token
        if not cursor:
            return
        try:
            system_arg = self.system_filter if self.asset_kind == _SEARCH_KIND_ROMS else ""
            result = endpoints.media_browse_list(
                self.api_client,
                self.asset_kind,
                q=self.search_query,
                system=system_arg,
                drones=self.drone_filter,
                exclude_local=bool(self.hide_local_items),
                cursor=cursor,
                limit=_BROWSE_PAGE_LIMIT,
            )
            self._apply_browse_page(result, append=True)
            self.browse_error = None
        except DroneApiError as error:
            self.browse_error = str(error)

    def _apply_browse_page(self, result, *, append: bool = False) -> None:
        if not isinstance(result, dict):
            if not append:
                self.browse_items = []
                self.next_page_token = ""
                self.has_more_pages = False
            return
        items = [dict(row) for row in (result.get("items") or []) if isinstance(row, dict)]
        if append:
            self.browse_items.extend(items)
        else:
            self.browse_items = items
            self.focused_row_index = 0
        self.next_page_token = str(result.get("next_cursor") or "")
        self.has_more_pages = bool(result.get("has_more")) and bool(self.next_page_token)

        for row in items:
            sys_name = str(row.get("system") or "").strip()
            if sys_name and sys_name not in self.available_systems:
                self.available_systems.append(sys_name)
        self.available_systems.sort()

        response_sources = [row for row in (result.get("sources") or []) if isinstance(row, dict)]
        notices = []
        seen_notice_ids = set()
        for src in response_sources:
            status = str(src.get("status") or "").strip().lower()
            if status in {"offline", "timeout", "unsupported", "error"}:
                name = str(src.get("name") or src.get("drone_name") or src.get("drone_id") or "Peer")
                src_id = str(src.get("drone_id") or name)
                if src_id not in seen_notice_ids:
                    seen_notice_ids.add(src_id)
                    notices.append(f"{name}: {status}")
        for src in self.browse_sources:
            if not isinstance(src, dict) or src.get("is_local"):
                continue
            src_id = str(src.get("drone_id") or "")
            name = str(src.get("name") or src.get("drone_name") or src_id or "Peer")
            status = str(src.get("status") or "").strip().lower()
            if status in {"offline", "timeout", "unsupported", "error"} and src_id not in seen_notice_ids:
                seen_notice_ids.add(src_id)
                notices.append(f"{name}: {status}")
            elif src.get("online") is False and src_id not in seen_notice_ids:
                seen_notice_ids.add(src_id)
                notices.append(f"{name}: offline")
        if notices or not append:
            self.browse_source_notices = notices

    def _queue_search_kind(self, kind: str, label: str) -> None:
        if self.asset_kind == kind and self.browse_items:
            return
        if self.defer_action(f"Searching {label.lower()}...", self._reload_search):
            self.asset_kind = kind
            self.system_filter = ""

    def _download_item(self, item: dict) -> None:
        label = _item_display_title(item)
        system = str(item.get("system") or "") if self.asset_kind == _SEARCH_KIND_ROMS else ""
        peer_id = str(item.get("drone_id") or self.request_peer_id or "")
        self._request_item(self.asset_kind, item, label, system=system, peer_id=peer_id)

    def _request_item(
        self,
        asset_type: str,
        item: dict,
        label: str,
        *,
        system: str = "",
        peer_id: str = "",
    ) -> None:
        key = (asset_type, _request_item_key(item))
        resolved_peer = peer_id or str(item.get("drone_id") or self.request_peer_id or "")
        resolved_system = system or (str(item.get("system") or "") if asset_type == _SEARCH_KIND_ROMS else "")

        def request_in_background() -> None:
            try:
                self._do_request_item(asset_type, item, label, resolved_system, peer_id=resolved_peer)
            finally:
                self._pending_request_key = None

        if self.defer_action(f"Starting download: {label}...", request_in_background):
            self._pending_request_key = key
            self.request_message = None

    def _do_request_item(
        self,
        asset_type: str,
        item: dict,
        label: str,
        system: str = "",
        *,
        peer_id: str = "",
    ) -> None:
        target_peer = peer_id or str(item.get("drone_id") or self.request_peer_id or "")
        resolved_system = system or (str(item.get("system") or "") if asset_type == _SEARCH_KIND_ROMS else "")
        ignore_existing = bool(self.hide_local_items or self.ignore_existing_games)
        try:
            result = endpoints.request_asset(
                self.api_client,
                target_peer,
                asset_type,
                item,
                system=resolved_system,
                ignore_existing=ignore_existing,
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
                self.request_message = f"Download started: {label}."
            self._next_download_poll_at = 0.0
        except DroneApiError as error:
            self.request_message = str(error)

    def _target_bulk_peer_ids(self) -> list[str]:
        if self.drone_filter.strip():
            return [pid.strip() for pid in self.drone_filter.split(",") if pid.strip()]
        peer_ids = []
        for item in self.browse_items:
            if not isinstance(item, dict) or item.get("is_local"):
                continue
            pid = str(item.get("drone_id") or "").strip()
            if pid and pid != "self" and pid not in peer_ids:
                peer_ids.append(pid)
        if peer_ids:
            return peer_ids
        for src in self.browse_sources:
            if not isinstance(src, dict) or src.get("is_local") or src.get("online") is False:
                continue
            pid = str(src.get("drone_id") or "").strip()
            if pid and pid != "self" and pid not in peer_ids:
                peer_ids.append(pid)
        if peer_ids:
            return peer_ids
        if self.request_peer_id:
            return [str(self.request_peer_id)]
        return []

    def _download_all(self) -> None:
        asset_type = self.asset_kind
        system = self.system_filter if asset_type == _SEARCH_KIND_ROMS else ""
        query = self.search_query
        ignore_existing = bool(self.hide_local_items or self.ignore_existing_games)
        peer_ids = self._target_bulk_peer_ids()
        if not peer_ids:
            self.request_message = "No online peer Drones available to download from."
            return
        try:
            before = endpoints.downloads(self.api_client)
            known_ids = {
                str(job.get("job_id") or job.get("id") or "")
                for job in (before.get("downloads") or [] if isinstance(before, dict) else [])
                if isinstance(job, dict)
            }
            queued_job_ids = []
            queued_job_rows = []
            skipped = 0
            queued = 0
            for peer_id in peer_ids:
                result = endpoints.request_assets_bulk(
                    self.api_client,
                    peer_id,
                    asset_type,
                    system=system or "",
                    query=query,
                    ignore_existing=ignore_existing,
                )
                if not isinstance(result, dict):
                    continue
                for value in result.get("queued_job_ids") or []:
                    if str(value):
                        queued_job_ids.append(str(value))
                for row in result.get("queued_jobs") or []:
                    if isinstance(row, dict):
                        queued_job_rows.append(row)
                skipped += int(result.get("skipped_existing") or 0)
                queued += int(result.get("queued_assets") or 0)

            after = endpoints.downloads(self.api_client)
            after_downloads = after.get("downloads") or [] if isinstance(after, dict) else []
            after_by_id = {
                str(job.get("job_id") or job.get("id") or ""): job
                for job in after_downloads
                if isinstance(job, dict) and (job.get("job_id") or job.get("id"))
            }
            if queued_job_rows:
                new_jobs = []
                for initial in queued_job_rows:
                    job_id = str(initial.get("job_id") or initial.get("id") or "")
                    current = after_by_id.get(job_id)
                    new_jobs.append(
                        {**initial, **current}
                        if current
                        else {**initial, "status": "completed", "percentage": 100.0}
                    )
            else:
                new_jobs = [
                    job
                    for job in after_downloads
                    if isinstance(job, dict)
                    and (
                        str(job.get("job_id") or job.get("id") or "") in queued_job_ids
                        if queued_job_ids
                        else str(job.get("job_id") or job.get("id") or "") not in known_ids
                    )
                    and str(job.get("source_drone_id") or "") in set(peer_ids)
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
            if not queued and primary_jobs:
                queued = len(primary_jobs)
            self.request_batch = {
                "asset_type": asset_type,
                "job_ids": batch_ids,
                "completed_missing_ids": completed_before_first_poll,
                "queued": queued,
                "skipped": skipped,
                "status": "completed" if not batch_ids else "queued",
            }
            noun = {"roms": "game", "movies": "movie", "music": "music"}.get(asset_type, "asset")
            self.request_message = f"Queued {queued} {noun} download(s); ignored {skipped} existing."
            if not batch_ids and skipped:
                self._mark_visible_existing_items_downloaded()
            self._next_download_poll_at = 0.0
        except DroneApiError as error:
            self.request_message = str(error)

    @staticmethod
    def _job_asset_type(job: dict) -> str:
        explicit = str(job.get("asset_type") or "").lower()
        if explicit in {"roms", "movies", "music"}:
            return explicit
        file_type = str(job.get("file_type") or "").lower()
        if file_type == "movie":
            return "movies"
        if file_type == "music":
            return "music"
        if file_type == "rom":
            return "roms"
        return explicit

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
        """Synchronous refresh retained for direct callers and unit tests."""
        try:
            snapshot = endpoints.downloads(self.api_client)
            self.request_queue_snapshot = snapshot if isinstance(snapshot, dict) else {}
            self._apply_download_snapshot(self.request_queue_snapshot)
            self.request_download_error = None
        except DroneApiError as error:
            self.request_download_error = str(error)
        finally:
            self._next_download_poll_at = time.monotonic() + _DOWNLOAD_POLL_SECONDS

    def _fetch_download_snapshot(self) -> None:
        try:
            result = endpoints.downloads(self.api_client)
            self._download_poll_result = result if isinstance(result, dict) else {}
            self._download_poll_error = None
        except DroneApiError as error:
            self._download_poll_result = None
            self._download_poll_error = str(error)
        except Exception as error:  # a background poll must never escape its thread
            self._download_poll_result = None
            self._download_poll_error = f"Download status unavailable: {error.__class__.__name__}"

    def _apply_download_snapshot(self, snapshot: dict) -> None:
        current_jobs = {
            str(job.get("job_id") or job.get("id") or ""): job
            for job in (snapshot.get("downloads") or [])
            if isinstance(job, dict) and (job.get("job_id") or job.get("id"))
        }
        for key, tracked in list(self.request_downloads.items()):
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
        for tracked in list(self.request_downloads.values()):
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
        _asset_type, item_key = key
        for row in self.browse_items:
            if _request_item_key(row) == item_key:
                row["exists_locally"] = True
                row["_downloaded"] = True

    def _mark_visible_existing_items_downloaded(self) -> None:
        for row in self.browse_items:
            if row.get("exists_locally"):
                row["_downloaded"] = True

    def _has_active_downloads(self) -> bool:
        return any(
            str(state.get("status") or "") not in _DOWNLOAD_TERMINAL_STATUSES
            for state in list(self.request_downloads.values())
        )

    def _maybe_poll_downloads(self) -> None:
        """Advance non-blocking download polling from the render thread."""
        poll = self._download_poll_thread
        if poll is not None:
            if poll.is_alive():
                return
            self._download_poll_thread = None
            if self._download_poll_result is not None:
                self.request_queue_snapshot = self._download_poll_result
                self._apply_download_snapshot(self.request_queue_snapshot)
                self.request_download_error = None
            elif self._download_poll_error:
                self.request_download_error = self._download_poll_error
            self._download_poll_result = None
            self._download_poll_error = None
            self._next_download_poll_at = time.monotonic() + _DOWNLOAD_POLL_SECONDS
            return
        if time.monotonic() < self._next_download_poll_at:
            return
        self._download_poll_result = None
        self._download_poll_error = None
        self._download_poll_thread = threading.Thread(
            target=self._fetch_download_snapshot,
            name="drone-ports-download-poll",
            daemon=True,
        )
        self._download_poll_thread.start()

    def _visible_browse_items(self) -> list[dict]:
        rows = list(self.browse_items)
        if not self.hide_local_items:
            return rows
        return [
            row
            for row in rows
            if not (row.get("exists_locally") or row.get("_downloaded"))
        ]

    def _download_queue_rows(self):
        snapshot = self.request_queue_snapshot or {}
        rows = []
        for group, key in (("Active", "active"), ("Queued", "queued"), ("Recent", "recent")):
            rows.extend(
                (group, job)
                for job in (snapshot.get(key) or [])
                if isinstance(job, dict)
            )
        if rows:
            rows.sort(key=lambda row: str(row[1].get("created_at") or ""), reverse=True)
            return rows
        rows = [
            ("Download", job)
            for job in (snapshot.get("downloads") or [])
            if isinstance(job, dict)
        ]
        rows.sort(key=lambda row: str(row[1].get("created_at") or ""), reverse=True)
        return rows

    def _draw_download_queue_panel(self) -> None:
        snapshot = self.request_queue_snapshot or {}
        active_count = len(snapshot.get("active") or [])
        queued_count = len(snapshot.get("queued") or [])
        recent_count = len(snapshot.get("recent") or [])

        imgui.spacing()
        imgui.separator()
        imgui.spacing()
        imgui.text("Download Queue")
        imgui.same_line()
        imgui.text_disabled(
            f"Active {active_count}  |  Queued {queued_count}  |  Recent {recent_count}"
        )
        imgui.text_disabled("Downloads continue in the background after this app is closed.")

        if self.request_download_error:
            imgui.text_colored(
                WARNING_COLOR,
                f"Download status unavailable: {self.request_download_error}",
            )

        rows = self._download_queue_rows()
        imgui.begin_child(
            "request_download_queue",
            imgui.ImVec2(0, 140),
            True,
        )
        if not rows:
            imgui.text_disabled("No queued, active, or recent downloads.")
        else:
            clipper = imgui.ListClipper()
            clipper.begin(len(rows))
            while clipper.step():
                for index in range(clipper.display_start, clipper.display_end):
                    group, job = rows[index]
                    self._draw_download_queue_row(group, job)
        imgui.end_child()

    def _draw_download_queue_row(self, group: str, job: dict) -> None:
        status = str(job.get("status") or "queued").lower()
        label = _download_job_label(job)
        system = str(job.get("system") or "")
        display_label = f"{label} ({system})" if system else label
        imgui.text(f"{group}: {display_label[:100]}")
        imgui.same_line()
        if status in {"failed", "cancelled"}:
            imgui.text_colored(ERROR_COLOR, status.title())
        elif status in {"completed", "skipped"}:
            imgui.text_colored(SUCCESS_COLOR, "Completed" if status == "completed" else "Skipped")
        elif status in {"queued", "pending", "paused"}:
            position = job.get("queue_position")
            suffix = f" #{position}" if position else ""
            imgui.text_colored(WARNING_COLOR, f"{status.title()}{suffix}")
        else:
            imgui.text(status.title())

        percentage = 100.0 if status in {"completed", "skipped"} else self._job_percentage(job)
        downloaded = job.get("downloaded_bytes") or job.get("bytes_transferred") or 0
        total = job.get("total_bytes") or job.get("file_size") or 0
        overlay = f"{percentage:.1f}%"
        progress_width = min(520.0, max(160.0, imgui.get_content_region_avail().x - 220.0))
        imgui.progress_bar(
            percentage / 100.0,
            imgui.ImVec2(progress_width, 0),
            overlay,
        )
        imgui.same_line()
        if total:
            detail = f"{_format_bytes(downloaded)} / {_format_bytes(total)}"
        else:
            detail = _format_bytes(downloaded)
        speed = job.get("transfer_speed_bps")
        if speed:
            detail += f"  {_format_bytes(speed)}/s"
        if status in {"failed", "cancelled"}:
            error = str(job.get("error_message") or job.get("failure_reason") or "")
            if error:
                detail = error[:100]
        imgui.text_disabled(detail)
        imgui.separator()

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

    def _draw_search(self) -> None:
        self._maybe_poll_downloads()

        for key, label in _SEARCH_KINDS:
            tab_button(
                label,
                active=self.asset_kind == key,
                on_click=lambda k=key, value=label: self._queue_search_kind(k, value),
            )
            imgui.same_line()
        imgui.new_line()
        imgui.spacing()

        self.search_query, triggered = search_box("search_assets_query", self.search_query)
        if triggered:
            self.defer_action("Searching assets...", self._reload_search)

        # Filter bar: System (for Games), Hide items I have, Drones, Download All
        if self.asset_kind == _SEARCH_KIND_ROMS:
            system_preview = self.system_filter if self.system_filter else "All systems"
            imgui.set_next_item_width(180)
            if imgui.begin_combo("System", system_preview):
                _, selected_all = imgui.selectable("All systems", self.system_filter == "")
                if selected_all and self.system_filter != "":
                    self.system_filter = ""
                    self.defer_action("Filtering by system...", self._reload_search)
                for sys_name in self.available_systems:
                    _, selected_sys = imgui.selectable(sys_name, self.system_filter == sys_name)
                    if selected_sys and self.system_filter != sys_name:
                        self.system_filter = sys_name
                        self.defer_action("Filtering by system...", self._reload_search)
                imgui.end_combo()
            imgui.same_line()

        changed_hide, new_hide = imgui.checkbox("Hide items I have", self.hide_local_items)
        if changed_hide:
            self.hide_local_items = new_hide
            self.ignore_existing_games = new_hide
            self.defer_action("Updating filter...", self._reload_search)

        imgui.same_line()
        drone_preview = "All online"
        if self.drone_filter:
            drone_preview = self.drone_filter
            for src in self.browse_sources:
                if isinstance(src, dict) and str(src.get("drone_id") or "") == self.drone_filter:
                    drone_preview = str(src.get("name") or src.get("drone_name") or self.drone_filter)
                    break
        imgui.set_next_item_width(180)
        if imgui.begin_combo("Drones", drone_preview):
            _, selected_all_drones = imgui.selectable("All online", self.drone_filter == "")
            if selected_all_drones and self.drone_filter != "":
                self.drone_filter = ""
                self.defer_action("Filtering by Drone...", self._reload_search)
            for src in self.browse_sources:
                if not isinstance(src, dict) or src.get("is_local"):
                    continue
                src_id = str(src.get("drone_id") or "")
                src_name = str(src.get("name") or src.get("drone_name") or src_id)
                if not src_id:
                    continue
                _, selected_drone = imgui.selectable(f"{src_name}##{src_id}", self.drone_filter == src_id)
                if selected_drone and self.drone_filter != src_id:
                    self.drone_filter = src_id
                    self.defer_action("Filtering by Drone...", self._reload_search)
            imgui.end_combo()

        imgui.same_line()
        can_download_all = bool(self._target_bulk_peer_ids()) and not self._has_active_downloads()
        imgui.begin_disabled(not can_download_all)
        if imgui.button("Download All"):
            self.defer_action("Preparing bulk download...", self._download_all)
        imgui.end_disabled()

        for notice in self.browse_source_notices:
            imgui.text_colored(WARNING_COLOR, f"Peer status: {notice}")

        if self.browse_error:
            imgui.text_colored(ERROR_COLOR, self.browse_error)

        visible_items = self._visible_browse_items()
        imgui.begin_child("search_assets_results", imgui.ImVec2(0, _LIST_HEIGHT), True)
        if not visible_items:
            imgui.text_disabled("No matching assets found across online paired Drones.")
        for index, item in enumerate(visible_items):
            self._draw_search_result_row(index, item)
        if self.has_more_pages and self.next_page_token:
            scroll_max = imgui.get_scroll_max_y()
            near_bottom = scroll_max > 0 and imgui.get_scroll_y() >= scroll_max - 32.0
            if imgui.button("Load More") or near_bottom:
                self.defer_action("Loading more results...", self._load_next_page)
        imgui.end_child()

        if visible_items and 0 <= self.focused_row_index < len(visible_items):
            focused_item = visible_items[self.focused_row_index]
            if imgui.is_key_pressed(imgui.Key.gamepad_face_left, False):
                self._download_item(focused_item)
            if imgui.is_key_pressed(imgui.Key.gamepad_face_top, False):
                self.details_item = focused_item
                self._details_popup_just_opened = True

        self._draw_details_popup()

        if self.request_message:
            imgui.spacing()
            imgui.text_disabled(self.request_message)
        self._draw_download_queue_panel()

    def _draw_search_result_row(self, index: int, item: dict) -> None:
        title = _item_display_title(item)
        meta = _item_metadata_label(self.asset_kind, item)
        drone_name = str(item.get("drone_name") or item.get("drone_id") or "")
        size_text = _format_bytes(item.get("file_size")) if item.get("file_size") else ""

        parts = [title]
        if meta:
            parts.append(f"[{meta}]")
        if drone_name:
            parts.append(f"({drone_name})")
        if size_text:
            parts.append(size_text)
        row_label = "   ".join(parts)

        _, clicked = imgui.selectable(
            f"{row_label}##result_row_{index}",
            self.focused_row_index == index,
            imgui.SelectableFlags_.allow_overlap.value,
        )
        if imgui.is_item_focused() or clicked:
            self.focused_row_index = index

        imgui.same_line()
        if self._draw_download_state(self.asset_kind, item):
            can_redownload = (
                item.get("exists_locally") or item.get("_downloaded")
            ) and not (self.hide_local_items or self.ignore_existing_games)
            if can_redownload:
                imgui.same_line()
                if imgui.button(f"Download Again##result_dl_{index}"):
                    self._download_item(item)
        elif self._pending_request_key == (self.asset_kind, _request_item_key(item)):
            widgets.spinner()
            imgui.same_line()
            imgui.text_disabled("Starting...")
        elif imgui.button(f"Download##result_dl_{index}"):
            self._download_item(item)

        imgui.same_line()
        if imgui.button(f"Details##result_info_{index}"):
            self.details_item = item
            self._details_popup_just_opened = True

    def _draw_details_popup(self) -> None:
        if self._details_popup_just_opened:
            imgui.open_popup(_DETAILS_POPUP_NAME)
            self._details_popup_just_opened = False

        if self.details_item is None:
            return

        opened, _ = imgui.begin_popup_modal(
            _DETAILS_POPUP_NAME,
            flags=imgui.WindowFlags_.always_auto_resize.value,
        )
        if not opened:
            return

        item = self.details_item
        imgui.text(_item_display_title(item))
        imgui.separator()
        for label, key in (
            ("Source Drone", "drone_name"),
            ("System", "system"),
            ("Artist", "artist"),
            ("Album", "album"),
            ("Show", "show_title"),
            ("Path", "file_path"),
        ):
            value = item.get(key)
            if value:
                imgui.text(f"{label}: {value}")
        if item.get("file_size"):
            imgui.text(f"Size: {_format_bytes(item.get('file_size'))}")
        imgui.spacing()

        imgui.set_item_default_focus()
        close_pressed = imgui.button("Close") or imgui.is_key_pressed(imgui.Key.gamepad_face_right)
        imgui.same_line()
        if imgui.button("Download"):
            self._download_item(item)
            self.details_item = None
            imgui.close_current_popup()
        elif close_pressed:
            self.details_item = None
            imgui.close_current_popup()

        imgui.end_popup()

    # --- draw ------------------------------------------------------------

    def draw(self, navigator) -> None:
        if not imgui.is_popup_open("", imgui.PopupFlags_.any_popup_id.value):
            if imgui.is_key_pressed(imgui.Key.gamepad_l2, False):
                self._step_sub_tab(-1)
            elif imgui.is_key_pressed(imgui.Key.gamepad_r2, False):
                self._step_sub_tab(1)

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
        else:
            self._draw_search()

        if self.tab == _TAB_SEARCH:
            button_hint_footer("A Select   B Back   X Download   Y Details   L2/R2 Sub-tab   L1/R1 Section")
        else:
            button_hint_footer("A Select   B Back   L2/R2 Sub-tab   L1/R1 Section")
