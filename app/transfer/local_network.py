"""Local-network control plane for peer discovery and explicit trust."""

from __future__ import annotations

import json
import os
import secrets
import socket
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from typing import Any, Callable, Optional

try:
    from ..storage.state_store import append_event, database_path, load_events, load_payload, save_payload
except ImportError:
    from storage.state_store import append_event, database_path, load_events, load_payload, save_payload  # type: ignore


MODE_OVERMIND = "overmind"
MODE_LOCAL_NETWORK = "local_network"
MODE_BOTH = "both"
MODE_DISABLED = "disabled"
VALID_MODES = {MODE_OVERMIND, MODE_LOCAL_NETWORK, MODE_BOTH, MODE_DISABLED}

DISCOVERY_SERVICE = "batocera-drone-local-v1"
DISCOVERY_GROUP = os.environ.get("DRONE_LOCAL_DISCOVERY_GROUP", "239.255.42.99")
DISCOVERY_PORT = int(os.environ.get("DRONE_LOCAL_DISCOVERY_PORT", "42042"))
DISCOVERY_INTERVAL_SECONDS = max(5, int(os.environ.get("DRONE_LOCAL_DISCOVERY_INTERVAL_SECONDS", "30")))
DISCOVERY_STALE_SECONDS = max(60, int(os.environ.get("DRONE_LOCAL_DISCOVERY_STALE_SECONDS", "300")))
PAIRING_CODE_MINUTES = max(1, int(os.environ.get("DRONE_LOCAL_PAIRING_CODE_MINUTES", "15")))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now_iso() -> str:
    return _now().isoformat()


def _db(settings: Any) -> Path:
    return database_path(Path(settings.userdata_root))


def get_integrations(settings: Any) -> dict:
    configured = str(os.environ.get("DRONE_NETWORK_MODE") or "").strip().lower()
    if configured in VALID_MODES:
        return {
            "overmind_enabled": configured in {MODE_OVERMIND, MODE_BOTH},
            "local_network_enabled": configured in {MODE_LOCAL_NETWORK, MODE_BOTH},
        }
    try:
        db_path = _db(settings)
        if not db_path.exists():
            return {"overmind_enabled": True, "local_network_enabled": False}
        integrations = load_payload(db_path, "integration_enablement", {})
        if isinstance(integrations, dict) and (
            "overmind_enabled" in integrations or "local_network_enabled" in integrations
        ):
            return {
                "overmind_enabled": bool(integrations.get("overmind_enabled")),
                "local_network_enabled": bool(integrations.get("local_network_enabled")),
            }
        payload = load_payload(db_path, "network_mode", {"mode": MODE_OVERMIND})
    except (AttributeError, TypeError, ValueError, OSError):
        return {"overmind_enabled": True, "local_network_enabled": False}
    mode = str(payload.get("mode") if isinstance(payload, dict) else payload or "").strip().lower()
    return {
        "overmind_enabled": mode in {MODE_OVERMIND, MODE_BOTH},
        "local_network_enabled": mode in {MODE_LOCAL_NETWORK, MODE_BOTH},
    }


def get_mode(settings: Any) -> str:
    integrations = get_integrations(settings)
    if integrations["overmind_enabled"] and integrations["local_network_enabled"]:
        return MODE_BOTH
    if integrations["local_network_enabled"]:
        return MODE_LOCAL_NETWORK
    if integrations["overmind_enabled"]:
        return MODE_OVERMIND
    return MODE_DISABLED


def set_integrations(settings: Any, *, overmind_enabled: bool, local_network_enabled: bool) -> dict:
    payload = {
        "overmind_enabled": bool(overmind_enabled),
        "local_network_enabled": bool(local_network_enabled),
        "updated_at": _now_iso(),
    }
    save_payload(_db(settings), "integration_enablement", payload)
    return {**payload, "mode": get_mode(settings)}


def set_mode(settings: Any, mode: str) -> dict:
    normalized = str(mode or "").strip().lower()
    if normalized not in VALID_MODES:
        raise ValueError("mode must be overmind, local_network, both, or disabled")
    return set_integrations(
        settings,
        overmind_enabled=normalized in {MODE_OVERMIND, MODE_BOTH},
        local_network_enabled=normalized in {MODE_LOCAL_NETWORK, MODE_BOTH},
    )


def is_local_mode(settings: Any) -> bool:
    return get_integrations(settings)["local_network_enabled"]


def is_overmind_mode(settings: Any) -> bool:
    return get_integrations(settings)["overmind_enabled"]


def _load_peer_map(settings: Any, namespace: str) -> dict[str, dict]:
    payload = load_payload(_db(settings), namespace, {})
    return {
        str(peer_id): dict(peer)
        for peer_id, peer in (payload.items() if isinstance(payload, dict) else [])
        if peer_id and isinstance(peer, dict)
    }


def _save_peer_map(settings: Any, namespace: str, peers: dict[str, dict]) -> None:
    save_payload(_db(settings), namespace, peers)


def discovered_peers(settings: Any, include_stale: bool = False) -> list[dict]:
    peers = _load_peer_map(settings, "local_discovered_peers")
    cutoff = _now() - timedelta(seconds=DISCOVERY_STALE_SECONDS)
    rows = []
    for peer in peers.values():
        try:
            seen = datetime.fromisoformat(str(peer.get("last_seen") or ""))
        except Exception:
            seen = datetime.min.replace(tzinfo=timezone.utc)
        if include_stale or seen >= cutoff:
            rows.append(peer)
    return sorted(rows, key=lambda row: (str(row.get("name") or "").lower(), str(row.get("drone_id") or "")))


def paired_peers(settings: Any) -> list[dict]:
    peers = _load_peer_map(settings, "local_paired_peers")
    return sorted(peers.values(), key=lambda row: (str(row.get("name") or "").lower(), str(row.get("drone_id") or "")))


def get_paired_peer(settings: Any, peer_id: str) -> Optional[dict]:
    return _load_peer_map(settings, "local_paired_peers").get(str(peer_id or "").strip())


def record_discovered_peer(settings: Any, payload: dict, source_ip: Optional[str] = None) -> Optional[dict]:
    if not is_local_mode(settings) or str(payload.get("service") or "") != DISCOVERY_SERVICE:
        return None
    peer_id = str(payload.get("drone_id") or "").strip()
    if not peer_id or peer_id == str(settings.overmind_device_id):
        return None
    peers = _load_peer_map(settings, "local_discovered_peers")
    existing = dict(peers.get(peer_id) or {})
    scheme = str(payload.get("scheme") or "https")
    api_port = int(payload.get("api_port") or 443)
    advertised_url = str(payload.get("reachable_url") or "")
    reachable_url = advertised_url
    if source_ip:
        suffix = "" if scheme == "https" and api_port == 443 else f":{api_port}"
        reachable_url = f"{scheme}://{source_ip}{suffix}"
    trusted_peer = get_paired_peer(settings, peer_id)
    peer = {
        **existing,
        "drone_id": peer_id,
        "device_id": peer_id,
        "name": str(payload.get("name") or peer_id),
        "hostname": str(payload.get("hostname") or ""),
        "reachable_url": reachable_url,
        "advertised_reachable_url": advertised_url,
        "scheme": scheme,
        "api_port": api_port,
        "certificate_fingerprint": str(payload.get("certificate_fingerprint") or ""),
        "source_ip": str(source_ip or existing.get("source_ip") or ""),
        "last_seen": _now_iso(),
        "paired": bool(trusted_peer),
    }
    peers[peer_id] = peer
    _save_peer_map(settings, "local_discovered_peers", peers)
    if trusted_peer:
        save_paired_peer(
            settings,
            {
                **trusted_peer,
                "name": peer["name"],
                "hostname": peer["hostname"],
                "reachable_url": peer["reachable_url"],
                "advertised_reachable_url": peer["advertised_reachable_url"],
                "scheme": peer["scheme"],
                "api_port": peer["api_port"],
                "source_ip": peer["source_ip"],
                "last_seen": peer["last_seen"],
            },
        )
    return peer


def save_paired_peer(settings: Any, peer: dict) -> dict:
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "").strip()
    if not peer_id or peer_id == str(settings.overmind_device_id):
        raise ValueError("invalid peer id")
    peers = _load_peer_map(settings, "local_paired_peers")
    stored = {
        **dict(peers.get(peer_id) or {}),
        **peer,
        "drone_id": peer_id,
        "device_id": peer_id,
        "paired": True,
        "paired_at": str(peer.get("paired_at") or _now_iso()),
        "last_seen": str(peer.get("last_seen") or _now_iso()),
    }
    peers[peer_id] = stored
    _save_peer_map(settings, "local_paired_peers", peers)
    discovered = _load_peer_map(settings, "local_discovered_peers")
    if peer_id in discovered:
        discovered[peer_id]["paired"] = True
        _save_peer_map(settings, "local_discovered_peers", discovered)
    return stored


def forget_peer(settings: Any, peer_id: str) -> bool:
    normalized = str(peer_id or "").strip()
    peers = _load_peer_map(settings, "local_paired_peers")
    removed = peers.pop(normalized, None) is not None
    _save_peer_map(settings, "local_paired_peers", peers)
    discovered = _load_peer_map(settings, "local_discovered_peers")
    if normalized in discovered:
        discovered[normalized]["paired"] = False
        _save_peer_map(settings, "local_discovered_peers", discovered)
    return removed


def load_peer_checks(settings: Any) -> list[dict]:
    payload = load_payload(_db(settings), "local_peer_checks", [])
    return payload if isinstance(payload, list) else []


def save_peer_checks(settings: Any, checks: list[dict]) -> None:
    save_payload(_db(settings), "local_peer_checks", checks)


def record_activity(settings: Any, activity: dict) -> None:
    append_event(_db(settings), "local_sync_activity", activity, max_events=250)


def load_activity(settings: Any, limit: int = 50) -> list[dict]:
    return load_events(_db(settings), "local_sync_activity", limit=max(1, min(int(limit), 250)))


def pairing_code(settings: Any, rotate: bool = False) -> dict:
    payload = load_payload(_db(settings), "local_pairing_code", {})
    valid = False
    if isinstance(payload, dict) and payload.get("code") and not rotate:
        try:
            valid = datetime.fromisoformat(str(payload.get("expires_at"))) > _now()
        except Exception:
            valid = False
    if not valid:
        payload = {
            "code": f"{secrets.randbelow(100_000_000):08d}",
            "created_at": _now_iso(),
            "expires_at": (_now() + timedelta(minutes=PAIRING_CODE_MINUTES)).isoformat(),
        }
        save_payload(_db(settings), "local_pairing_code", payload)
    return dict(payload)


def validate_pairing_code(settings: Any, value: str) -> bool:
    expected = pairing_code(settings)
    return secrets.compare_digest(str(expected.get("code") or ""), str(value or "").strip())


def discovery_payload(settings: Any, certificate_fingerprint: str = "") -> dict:
    scheme = "http" if settings.http_only else "https"
    port = int(settings.advertised_api_port or settings.https_port or 443)
    hostname = socket.gethostname()
    local_hostname = hostname if hostname.lower().endswith(".local") else f"{hostname}.local"
    suffix = "" if scheme == "https" and port == 443 else f":{port}"
    return {
        "service": DISCOVERY_SERVICE,
        "kind": "announce",
        "drone_id": str(settings.overmind_device_id),
        "name": hostname,
        "hostname": hostname,
        "scheme": scheme,
        "api_port": port,
        "reachable_url": f"{scheme}://{local_hostname}{suffix}",
        "certificate_fingerprint": certificate_fingerprint,
        "sent_at": _now_iso(),
    }


def announce(settings: Any, certificate_fingerprint: str = "") -> bool:
    if not is_local_mode(settings):
        return False
    data = json.dumps(discovery_payload(settings, certificate_fingerprint)).encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.sendto(data, (DISCOVERY_GROUP, DISCOVERY_PORT))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def start_discovery_worker(
    settings: Any,
    certificate_fingerprint: Callable[[], str],
    on_discovery: Optional[Callable[[dict], None]] = None,
) -> Thread:
    """Listen for multicast announcements and periodically advertise this Drone."""

    def run() -> None:
        sock: Optional[socket.socket] = None
        last_announce = 0.0
        while True:
            if not is_local_mode(settings):
                if sock is not None:
                    sock.close()
                    sock = None
                time.sleep(2)
                continue
            if sock is None:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if hasattr(socket, "SO_REUSEPORT"):
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                    sock.bind(("", DISCOVERY_PORT))
                    membership = struct.pack("4s4s", socket.inet_aton(DISCOVERY_GROUP), socket.inet_aton("0.0.0.0"))
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
                    sock.settimeout(1.0)
                except OSError:
                    if sock is not None:
                        sock.close()
                    sock = None
                    time.sleep(5)
                    continue
            now = time.monotonic()
            if now - last_announce >= DISCOVERY_INTERVAL_SECONDS:
                try:
                    announce(settings, certificate_fingerprint())
                except OSError:
                    pass
                last_announce = now
            try:
                raw, address = sock.recvfrom(65535)
                payload = json.loads(raw.decode("utf-8"))
                peer = record_discovered_peer(settings, payload, address[0])
                if peer and on_discovery:
                    on_discovery(peer)
            except socket.timeout:
                continue
            except (OSError, ValueError, json.JSONDecodeError):
                continue

    thread = Thread(target=run, name="drone-local-discovery", daemon=True)
    thread.start()
    return thread
