"""Select preferred online peers for Drone-to-Drone asset transfers."""

from __future__ import annotations

from typing import Optional


def select_best_peer(
    swarm: list,
    peer_checks: list,
    local_device_id: str,
    *,
    source_device_ids: Optional[set] = None,
    required_system: Optional[str] = None,
    allow_relay: bool = True,
) -> Optional[dict]:
    """Return the healthiest permitted peer, preferring sampled upload speed.

    A peer is a candidate if it is **directly reachable** (public_resolvable +
    public_reachable_url) or, when ``allow_relay``, **relay-reachable** via the
    Edge (edge_online). Directly-reachable peers are strongly preferred so relay
    is chosen only when no direct path exists; the final direct-vs-relay decision
    at transfer time belongs to the TransportSelector (direct first, relay on
    failure), so a peer marked directly reachable here can still fall back to
    relay if the direct connection actually fails.
    """
    checks = {str(row.get("target_drone_id") or ""): row for row in peer_checks if isinstance(row, dict)}
    allowed_sources = {str(item) for item in (source_device_ids or set()) if str(item)}
    candidates = []
    for peer in swarm if isinstance(swarm, list) else []:
        peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
        if not peer_id or peer_id == local_device_id or not peer.get("online", True):
            continue
        if allowed_sources and peer_id not in allowed_sources:
            continue
        if required_system:
            systems = peer.get("rom_systems") or peer.get("systems") or []
            system_names = {str(item.get("name") if isinstance(item, dict) else item).lower() for item in systems}
            if system_names and required_system.lower() not in system_names:
                continue
        check = checks.get(peer_id) or {}
        own_probe_address = str(check.get("target_address") or "").strip()
        if check.get("status") == "pass" and own_probe_address and not (
            peer.get("public_resolvable") and str(peer.get("public_reachable_url") or "").strip()
        ):
            # This drone's OWN probe reached the peer even though the swarm
            # snapshot says it isn't publicly resolvable. Our own measurement is
            # the ground truth for OUR connectivity (Overmind's flag reflects an
            # AWS-side probe and can lag or flap), so treat the peer as directly
            # reachable at the address we actually probed -- on an enriched copy,
            # so downstream address resolution (_peer_address) uses it too.
            peer = {**peer, "public_resolvable": True, "public_reachable_url": own_probe_address}
        directly_reachable = bool(peer.get("public_resolvable")) and bool(
            str(peer.get("public_reachable_url") or "").strip()
        )
        relay_reachable = bool(allow_relay) and bool(peer.get("edge_online"))
        if directly_reachable and check.get("status") == "fail":
            # Known-bad direct path: keep the peer only if relay can carry it.
            directly_reachable = False
        if not directly_reachable and not relay_reachable:
            continue
        score = 0.0
        try:
            score += float((peer.get("last_speed_sample") or {}).get("upload_mbps") or 0)
        except Exception:
            pass
        if directly_reachable:
            score += 2000
            if check.get("status") == "pass":
                score += 1000
        else:
            score += 500  # relay-reachable only
        candidates.append((score, peer_id, peer))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2] if candidates else None
