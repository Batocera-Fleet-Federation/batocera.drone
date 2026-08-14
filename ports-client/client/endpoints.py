"""Thin typed wrappers over specific Drone API endpoints.

Scoped to swarm-related activities only: show the swarm, connect to it
(Tailnet/Local Network), reference a peer's ROMs, download ROMs (and
Movies, part of the same pull-from-peer feature) from a peer, VPN, and
Backups. Local Assets browsing and the Debug/Automation admin tiles were
deliberately removed (see ports-client/README.md) -- use the browser UI
for those.
"""

from urllib.parse import quote

from .http_client import DroneApiClient


def swarm_overview(client: DroneApiClient) -> dict:
    return client.get("/admin/swarm/overview")


# --- Tailnet (join an existing swarm over Tailscale) ------------------------
# rotate-auth-key/sharing/pull-from-peer aren't wrapped: those manage an
# *existing* enrollment's credential-sharing story, not the "connect this
# drone" action that was actually asked for -- same scope line VPN draws
# around its own peer-sharing controls.

def tailnet_status(client: DroneApiClient) -> dict:
    return client.get("/admin/tailnet/status")


def tailnet_enroll(client: DroneApiClient, auth_key: str) -> dict:
    return client.post("/admin/tailnet/enroll", {"auth_key": auth_key})


# --- Local Network (pair with a nearby drone over LAN) ----------------------
# forget/dismiss (managing peers already paired or discovered-but-unwanted)
# aren't wrapped: this is the "connect" flow, not peer housekeeping -- see
# the LAN screen's docstring.

def local_network_status(client: DroneApiClient) -> dict:
    return client.get("/admin/local-network/status")


def local_network_discover(client: DroneApiClient) -> dict:
    return client.post("/admin/local-network/discover")


def local_network_rotate_pairing_code(client: DroneApiClient) -> dict:
    return client.post("/admin/local-network/pairing-code/rotate")


def local_network_pair(client: DroneApiClient, peer_id: str, pairing_code: str) -> dict:
    return client.post(f"/admin/local-network/peers/{quote(peer_id, safe='')}/pair", {"pairing_code": pairing_code})


# --- Reference ROMs (mount a paired peer's ROM/BIOS library via network share) ---

def network_shares(client: DroneApiClient) -> dict:
    return client.get("/admin/network-shares")


def network_share_enable(client: DroneApiClient, peer_id: str) -> dict:
    return client.post(f"/admin/network-shares/{quote(peer_id, safe='')}/enable")


def network_share_disable(client: DroneApiClient, peer_id: str) -> dict:
    return client.post(f"/admin/network-shares/{quote(peer_id, safe='')}/disable")


# --- Request Assets (browse a paired peer's inventory and pull an item) ---------
# Peer inventory has no "music" type at all (only roms/bios/artwork/saves/movies/
# config_backups/emulator_configs/gameplay -- see handlers_peer.py), and no
# dedicated "systems" listing either -- system+count comes from "summary"'s
# system_counts. The Request Assets screen is scoped to what the backend
# actually supports: Systems+ROMs and Movies.

_PEER_ASSET_BROWSE_TIMEOUT_SECONDS = 40

def peer_asset_summary(client: DroneApiClient, peer_id: str) -> dict:
    return client.get(
        f"/admin/local-network/peers/{quote(peer_id, safe='')}/assets?type=summary",
        timeout=_PEER_ASSET_BROWSE_TIMEOUT_SECONDS,
    )


def peer_roms(client: DroneApiClient, peer_id: str, system: str, *, limit: int = 200, offset: int = 0, query: str = "") -> dict:
    q = f"&q={quote(query)}" if query else ""
    return client.get(
        f"/admin/local-network/peers/{quote(peer_id, safe='')}/assets"
        f"?type=roms&system={quote(system, safe='')}&limit={int(limit)}&offset={int(offset)}{q}",
        timeout=_PEER_ASSET_BROWSE_TIMEOUT_SECONDS,
    )


def peer_movies(client: DroneApiClient, peer_id: str, *, limit: int = 200, offset: int = 0, query: str = "") -> dict:
    q = f"&q={quote(query)}" if query else ""
    return client.get(
        f"/admin/local-network/peers/{quote(peer_id, safe='')}/assets?type=movies&limit={int(limit)}&offset={int(offset)}{q}",
        timeout=_PEER_ASSET_BROWSE_TIMEOUT_SECONDS,
    )


_REQUEST_ASSET_TIMEOUT_SECONDS = 30


def request_asset(
    client: DroneApiClient,
    peer_id: str,
    asset_type: str,
    item: dict,
    *,
    system: str = "",
    ignore_existing: bool = True,
) -> dict:
    # This is a queue-only call (202, near-instant in principle -- the real
    # transfer runs later in the background over Drone-to-Drone P2P, not
    # through this request at all), but a real incident showed it can still
    # occasionally outrun the client's normal 15s budget when the local
    # Drone is mid poll-cycle (concurrent ROM/BIOS metadata scanning) --
    # the transfer itself had already succeeded server-side by the time the
    # client gave up and reported "could not reach Drone". A longer budget
    # here is cheap insurance against that same false-negative.
    body = {"peer_id": peer_id, "asset_type": asset_type, "item": item}
    if not ignore_existing:
        body["overwrite_files"] = True
    if system:
        body["system"] = system
    return client.post("/admin/local-network/sync", body, timeout=_REQUEST_ASSET_TIMEOUT_SECONDS)


_REQUEST_ASSET_BULK_TIMEOUT_SECONDS = 180


def request_assets_bulk(
    client: DroneApiClient,
    peer_id: str,
    asset_type: str,
    *,
    system: str = "",
    query: str = "",
    ignore_existing: bool = True,
) -> dict:
    body = {
        "peer_id": peer_id,
        "asset_type": asset_type,
    }
    if not ignore_existing:
        body["overwrite_files"] = True
    if system:
        body["system"] = system
    if query:
        body["q"] = query
    return client.post("/admin/local-network/sync-bulk", body, timeout=_REQUEST_ASSET_BULK_TIMEOUT_SECONDS)


def downloads(client: DroneApiClient) -> dict:
    return client.get("/admin/downloads")


# --- VPN ---------------------------------------------------------------
# Credentials/sharing/pull-from-peer/local-folder-import are wrapped now
# that ports-client's virtual keyboard makes typed secrets enterable via
# gamepad -- see the VPN screen's docstring for the two ways it now offers
# to actually get a .ovpn config onto the device (peer-pull, no typing or
# file picker at all; or a local drop-folder browse, see Part 4's new
# backend endpoints). vpn_upload exists for API completeness/testability;
# the screen itself never calls it -- no file-picker UI, by design.

def vpn_status(client: DroneApiClient) -> dict:
    return client.get("/admin/vpn")


def vpn_connect(client: DroneApiClient) -> dict:
    return client.post("/admin/vpn/connect")


def vpn_disconnect(client: DroneApiClient) -> dict:
    return client.post("/admin/vpn/disconnect")


def vpn_upload(client: DroneApiClient, filename: str, content: bytes) -> dict:
    return client.post_multipart("/admin/vpn/upload", "config", filename, content)


def vpn_credentials(client: DroneApiClient, username: str, password: str) -> dict:
    return client.post("/admin/vpn/credentials", {"username": username, "password": password})


def vpn_sharing(client: DroneApiClient, enabled: bool) -> dict:
    return client.post("/admin/vpn/sharing", {"enabled": enabled})


def vpn_pull_from_peer(client: DroneApiClient, peer_id: str) -> dict:
    return client.post("/admin/vpn/pull-from-peer", {"peer_id": peer_id})


def vpn_list_import_files(client: DroneApiClient) -> dict:
    return client.get("/admin/vpn/import-folder")


def vpn_import_from_folder(client: DroneApiClient, filename: str) -> dict:
    return client.post("/admin/vpn/import-folder/apply", {"filename": filename})


# --- Backups -------------------------------------------------------------

def config_backups(client: DroneApiClient) -> dict:
    return client.get("/admin/config-backups")


def create_config_backup(client: DroneApiClient, *, name: str = "", description: str = "") -> dict:
    return client.post("/admin/config-backups", {"name": name, "description": description})


_APPLY_CONFIG_BACKUP_TIMEOUT_SECONDS = 180


def apply_config_backup(client: DroneApiClient, backup_id: int) -> dict:
    # Stops EmulationStation, copies the backup's files, then restarts it --
    # comfortably past the client's normal 15s default on real hardware, so
    # this needs its own generous budget or a slow-but-successful apply would
    # surface as a client-side "could not reach Drone" timeout instead of the
    # real result.
    return client.post(f"/admin/config-backups/{backup_id}/apply", {}, timeout=_APPLY_CONFIG_BACKUP_TIMEOUT_SECONDS)
