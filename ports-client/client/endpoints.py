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

def peer_asset_summary(client: DroneApiClient, peer_id: str) -> dict:
    return client.get(f"/admin/local-network/peers/{quote(peer_id, safe='')}/assets?type=summary")


def peer_roms(client: DroneApiClient, peer_id: str, system: str, *, limit: int = 200, offset: int = 0, query: str = "") -> dict:
    q = f"&q={quote(query)}" if query else ""
    return client.get(
        f"/admin/local-network/peers/{quote(peer_id, safe='')}/assets"
        f"?type=roms&system={quote(system, safe='')}&limit={int(limit)}&offset={int(offset)}{q}"
    )


def peer_movies(client: DroneApiClient, peer_id: str, *, limit: int = 200, offset: int = 0, query: str = "") -> dict:
    q = f"&q={quote(query)}" if query else ""
    return client.get(
        f"/admin/local-network/peers/{quote(peer_id, safe='')}/assets?type=movies&limit={int(limit)}&offset={int(offset)}{q}"
    )


def request_asset(client: DroneApiClient, peer_id: str, asset_type: str, item: dict, *, system: str = "") -> dict:
    body = {"peer_id": peer_id, "asset_type": asset_type, "item": item}
    if system:
        body["system"] = system
    return client.post("/admin/local-network/sync", body)


# --- VPN ---------------------------------------------------------------
# Upload/credentials/sharing/pull-from-peer are deliberately not wrapped:
# they're form-heavy admin actions with no practical gamepad-only UI (a
# .ovpn file picker, typed credentials) -- see the VPN screen's docstring.

def vpn_status(client: DroneApiClient) -> dict:
    return client.get("/admin/vpn")


def vpn_connect(client: DroneApiClient) -> dict:
    return client.post("/admin/vpn/connect")


def vpn_disconnect(client: DroneApiClient) -> dict:
    return client.post("/admin/vpn/disconnect")


# --- Backups -------------------------------------------------------------

def config_backups(client: DroneApiClient) -> dict:
    return client.get("/admin/config-backups")


def create_config_backup(client: DroneApiClient, *, name: str = "", description: str = "") -> dict:
    return client.post("/admin/config-backups", {"name": name, "description": description})
