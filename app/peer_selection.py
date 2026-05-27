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
) -> Optional[dict]:
    """Return the healthiest permitted peer, preferring sampled upload speed."""
    checks = {str(row.get("target_drone_id") or ""): row for row in peer_checks if isinstance(row, dict)}
    allowed_sources = {str(item) for item in (source_device_ids or set()) if str(item)}
    candidates = []
    for peer in swarm if isinstance(swarm, list) else []:
        peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
        if not peer_id or peer_id == local_device_id or not peer.get("online", True):
            continue
        if not peer.get("public_resolvable") or not str(peer.get("public_reachable_url") or "").strip():
            continue
        if allowed_sources and peer_id not in allowed_sources:
            continue
        if required_system:
            systems = peer.get("rom_systems") or peer.get("systems") or []
            system_names = {str(item.get("name") if isinstance(item, dict) else item).lower() for item in systems}
            if system_names and required_system.lower() not in system_names:
                continue
        check = checks.get(peer_id) or {}
        if check.get("status") == "fail":
            continue
        score = 0.0
        try:
            score += float((peer.get("last_speed_sample") or {}).get("upload_mbps") or 0)
        except Exception:
            pass
        if check.get("status") == "pass":
            score += 1000
        candidates.append((score, peer_id, peer))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2] if candidates else None
