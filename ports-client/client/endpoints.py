"""Thin typed wrappers over specific Drone API endpoints.

Scoped to swarm-related activities only: show the swarm, connect to it
(Tailnet/Local Network), search and download assets across paired Drones
(Games, Movies & Shows, and Music), VPN, and Backups. Local Assets browsing
and the Debug/Automation admin tiles were deliberately removed (see
ports-client/README.md) -- use the browser UI for those.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from .http_client import DroneApiClient


def swarm_overview(client: DroneApiClient, *, probe: bool = True) -> dict:
    # probe=0 is the stored-state snapshot. The Swarm screen uses it so the
    # list can paint before any peer is dialed; other callers keep the
    # probing default.
    path = "/admin/swarm/overview" if probe else "/admin/swarm/overview?probe=0"
    return client.get(path)


def swarm_probe_peer(client: DroneApiClient, peer_id: str) -> dict:
    # Server budget is 10s for inventory plus a 1.5s health fallback. The
    # client's 15s default can fire first and turn a slow drone into a
    # transport error, so this call gets its own headroom.
    return client.get(
        f"/admin/swarm/peers/{quote(peer_id, safe='')}/probe",
        timeout=20,
    )


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


# --- Search Assets (browse across online paired Drones and pull items) ------

_MEDIA_BROWSE_TIMEOUT_SECONDS = 40


def media_browse_sources(client: DroneApiClient) -> dict:
    return client.get("/media-browse/sources")


def media_browse_list(
    client: DroneApiClient,
    kind: str,
    *,
    q: str = "",
    system: str = "",
    drones: str | list[str] | tuple[str, ...] | set[str] = "",
    exclude_local: bool = False,
    cursor: str = "",
    limit: int = 50,
) -> dict:
    if isinstance(drones, (list, tuple, set)):
        drones_value = ",".join(str(item).strip() for item in drones if str(item or "").strip())
    else:
        drones_value = str(drones or "").strip()
    params = {
        "q": str(q or ""),
        "system": str(system or ""),
        "drones": drones_value,
        "exclude_local": "1" if exclude_local else "0",
        "cursor": str(cursor or ""),
        "limit": str(max(1, int(limit or 50))),
    }
    safe_kind = quote(str(kind or "roms").strip(), safe="")
    return client.get(f"/media-browse/{safe_kind}?{urlencode(params)}")


_REQUEST_ASSET_TIMEOUT_SECONDS = 30


def request_asset(
    client: DroneApiClient,
    peer_id: str,
    asset_type: str,
    item: dict,
    *,
    system: str = "",
    ignore_existing: bool = True,
    include_artwork: bool = True,
) -> dict:
    # This is a queue-only call (202, near-instant in principle -- the real
    # transfer runs later in the background over Drone-to-Drone P2P, not
    # through this request at all), but a real incident showed it can still
    # occasionally outrun the client's normal 15s budget when the local
    # Drone is mid poll-cycle (concurrent ROM/BIOS metadata scanning) --
    # the transfer itself had already succeeded server-side by the time the
    # client gave up and reported "could not reach Drone". A longer budget
    # here is cheap insurance against that same false-negative.
    body = {
        "peer_id": peer_id,
        "asset_type": asset_type,
        "item": item,
        "include_artwork": bool(include_artwork),
    }
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
