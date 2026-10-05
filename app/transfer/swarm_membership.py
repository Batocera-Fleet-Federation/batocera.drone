"""Swarm-wide membership propagated from an approved pairing.

Pairing is still the only way into the swarm: a discovered Drone is not a
member, and a certificate that fails the existing fingerprint pin is not
stored. Once two Drones approve a pairing, each one already trusts the
other, so that approval is shared with the peers they already trust. Every
Drone keeps its own paired-peer list; there is no central roster.

Removals use the same path. A newer removal beats an older add, and the
removed Drone is told with the certificate that was pinned while it was
still a member so it drops out instead of staying on its own roster.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from ..storage.state_store import database_path, load_payload, save_payload
    from . import local_network as _local_network
    from .drone_network import _certificate_pem_fingerprint
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path, load_payload, save_payload  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.drone_network import _certificate_pem_fingerprint  # type: ignore


MEMBERSHIP_NAMESPACE = "swarm_membership"
MAX_FORWARD_HOP = 3
_MAX_RECORDS = 64
_MAX_PEM_CHARS = 20000
_CARD_FIELDS = (
    "name",
    "hostname",
    "reachable_url",
    "advertised_reachable_url",
    "scheme",
    "api_port",
    "peer_mtls_port",
    "tailnet_ip",
    "source_ip",
    "pairing_source",
    "certificate_fingerprint",
    "certificate_pem",
)

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_poster: Optional[Callable[[Any, dict, dict], dict]] = None


class MembershipRejected(Exception):
    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status = status
        self.error = error


def set_membership_poster(poster: Optional[Callable[[Any, dict, dict], dict]]) -> None:
    """Test hook. Production posts over the paired peer's mTLS channel."""
    global _poster
    _poster = poster


def record_approved_member(settings: Any, peer: dict) -> None:
    """Remember a member that just passed the explicit pairing handshake."""
    peer_id = _peer_id(peer)
    if not peer_id or peer_id == str(settings.device_id):
        return
    card = _card_from_peer(settings, peer)

    def mutate(state: dict) -> None:
        state["departed"] = False
        _store(
            state,
            {
                "peer_id": peer_id,
                "op": "add",
                "epoch": _mint(state),
                "origin": str(settings.device_id),
                "card": card,
            },
        )
        _mark_dirty(state)

    _with_state(settings, mutate)


def publish_forget(settings: Any, peer_id: str) -> bool:
    """Drop a member locally and push that removal until the swarm agrees."""
    normalized = str(peer_id or "").strip()
    snapshot = _local_network.get_paired_peer(settings, normalized)
    removed = _local_network.forget_peer(settings, normalized)
    if not removed:
        return False
    if snapshot:
        try:
            _post_to_peer(settings, snapshot, hop=0)
        except Exception:
            pass
        _unlink_cert(settings, normalized)
    propagate_membership(settings, hop=0, skip_ids={normalized})
    return True


def propagate_membership(settings: Any, *, hop: int = 0, skip_ids: Optional[set[str]] = None) -> None:
    """Push this Drone's membership view to paired peers and merge replies.

    Forwards stop after ``MAX_FORWARD_HOP`` so a sync cannot chase itself
    around the swarm. Failures stay dirty so the health loop tries again.
    """
    if hop >= MAX_FORWARD_HOP or not _local_network.is_local_mode(settings):
        return
    if _departed(settings):
        return
    captured = _dirty_gen(settings)
    failed = False
    dialed = set(skip_ids or ())
    for _round in range(4):
        peers = [
            peer
            for peer in _local_network.paired_peers(settings)
            if _peer_id(peer) and _peer_id(peer) not in dialed
        ]
        if not peers:
            break
        for peer in peers:
            peer_id = _peer_id(peer)
            dialed.add(peer_id)
            try:
                response = _post_to_peer(settings, peer, hop=hop)
            except Exception:
                failed = True
                continue
            if not isinstance(response, dict):
                failed = True
                continue
            if str(response.get("status") or "") == "removed":
                _apply_departure_records(settings, response.get("records"))
                return
            try:
                _accept_records(settings, response.get("records"), peer_id)
            except MembershipRejected:
                failed = True
    if not failed:
        _mark_forwarded(settings, captured)


def handle_membership_request(settings: Any, payload: dict, caller_id: str) -> dict:
    """Apply one paired caller's view and return ours.

    ``caller_id`` is the mTLS-authenticated peer, or in the insecure HTTP
    test mode the claimed introducer. Callers that are not current members
    learn only that they were removed; their records are ignored.
    """
    if not _local_network.is_local_mode(settings):
        raise MembershipRejected(409, "Drone is not in local network mode")
    if not isinstance(payload, dict):
        raise MembershipRejected(400, "membership payload must be an object")
    caller = str(caller_id or "").strip()
    if not caller or _local_network.get_paired_peer(settings, caller) is None:
        tombstone = _tombstone_for(settings, caller)
        if tombstone is not None and caller:
            return _removed_response(settings, tombstone)
        raise MembershipRejected(403, "paired client certificate required")
    hop = _hop(payload)
    changed, removals = _accept_records(settings, payload.get("records"), caller)
    for snapshot in removals:
        removed_id = _peer_id(snapshot)
        try:
            _post_to_peer(settings, snapshot, hop=min(hop + 1, MAX_FORWARD_HOP))
        except Exception:
            pass
        _unlink_cert(settings, removed_id)
    if hop < MAX_FORWARD_HOP and (changed or _is_dirty(settings)):
        propagate_membership(settings, hop=hop + 1, skip_ids={caller})
    view = membership_view(settings)
    view["status"] = "synced"
    return view


def membership_view(settings: Any) -> dict:
    """Serializable roster this Drone is willing to attest."""

    def read(state: dict) -> dict:
        records = []
        for peer_id, record in _public_records(settings, state).items():
            records.append(_public_record(peer_id, record))
        records.sort(key=lambda item: item["peer_id"])
        return {"clock": int(state.get("clock") or 0), "records": records}

    return _with_state(settings, read)


def _accept_records(settings: Any, records: Any, introducer_id: str) -> tuple[bool, list[dict]]:
    introducer = str(introducer_id or "").strip()
    if _local_network.get_paired_peer(settings, introducer) is None:
        raise MembershipRejected(403, "membership introduction is not from a paired Drone")
    parsed = _parse_records(records)

    def mutate(state: dict) -> tuple[bool, list[dict]]:
        if state.get("departed"):
            return False, []
        own_id = str(settings.device_id)
        # A removal of this Drone ends its membership. Later adds in the same
        # payload must not be stored, or the next reconcile would rejoin it.
        for record in parsed:
            if record["peer_id"] != own_id or record["op"] != "remove":
                continue
            current = state["records"].get(own_id)
            if not _beats(record, current):
                continue
            state["departed"] = True
            state["clock"] = max(int(state.get("clock") or 0), int(record["epoch"]))
            state["records"][own_id] = record
            for peer in list(_local_network.paired_peers(settings)):
                _local_network.forget_peer(settings, _peer_id(peer), publish_membership=False)
                _unlink_cert(settings, _peer_id(peer))
            return True, []
        changed = False
        removals: list[dict] = []
        for record in parsed:
            peer_id = record["peer_id"]
            if peer_id == own_id:
                continue
            current = state["records"].get(peer_id)
            if not _beats(record, current):
                continue
            if record["op"] == "remove":
                snapshot = _local_network.get_paired_peer(settings, peer_id)
                state["clock"] = max(int(state.get("clock") or 0), int(record["epoch"]))
                state["records"][peer_id] = record
                if snapshot:
                    _local_network.forget_peer(settings, peer_id, publish_membership=False)
                    removals.append(snapshot)
                changed = True
                continue
            if not _apply_add(settings, state, record):
                continue
            changed = True
        return changed, removals

    return _with_state(settings, mutate)


def _apply_add(settings: Any, state: dict, record: dict) -> bool:
    card = record.get("card") if isinstance(record.get("card"), dict) else {}
    pem = str(card.get("certificate_pem") or "")
    if "BEGIN CERTIFICATE" not in pem or len(pem) > _MAX_PEM_CHARS:
        return False
    try:
        fingerprint = _certificate_pem_fingerprint(pem).lower()
    except Exception:
        return False
    claimed = str(card.get("certificate_fingerprint") or fingerprint).strip().lower()
    if claimed != fingerprint:
        return False
    peer_id = record["peer_id"]
    existing = _local_network.get_paired_peer(settings, peer_id) or {}
    pinned = str(existing.get("certificate_fingerprint") or "").strip().lower()
    if pinned and pinned != fingerprint:
        return False
    try:
        from .peer_connectivity import _save_local_peer_certificate
    except ImportError:  # pragma: no cover
        from transfer.peer_connectivity import _save_local_peer_certificate  # type: ignore
    cert_path, stored_fingerprint = _save_local_peer_certificate(settings, peer_id, pem)
    if stored_fingerprint.lower() != fingerprint:
        cert_path.unlink(missing_ok=True)
        return False
    peer = {
        "drone_id": peer_id,
        "name": str(card.get("name") or peer_id),
        "hostname": str(card.get("hostname") or ""),
        "reachable_url": str(card.get("reachable_url") or ""),
        "advertised_reachable_url": str(card.get("advertised_reachable_url") or ""),
        "scheme": str(card.get("scheme") or "https"),
        "api_port": _port(card.get("api_port"), 443),
        "peer_mtls_port": _port(card.get("peer_mtls_port"), _port(card.get("api_port"), 443)),
        "tailnet_ip": str(card.get("tailnet_ip") or ""),
        "source_ip": str(card.get("source_ip") or ""),
        "certificate_fingerprint": stored_fingerprint,
        "certificate_path": str(cert_path),
    }
    if not existing:
        peer["pairing_source"] = "swarm"
    elif existing.get("pairing_source"):
        peer["pairing_source"] = existing.get("pairing_source")
    if peer["scheme"] not in {"http", "https"} or not peer["reachable_url"]:
        cert_path.unlink(missing_ok=True)
        return False
    state["clock"] = max(int(state.get("clock") or 0), int(record["epoch"]))
    state["records"][peer_id] = {
        "op": "add",
        "epoch": int(record["epoch"]),
        "origin": record["origin"],
        "card": {**card, "certificate_fingerprint": stored_fingerprint, "certificate_pem": pem},
    }
    _local_network.save_paired_peer(settings, peer)
    _activate_certificate(cert_path)
    return True


def _apply_departure_records(settings: Any, records: Any) -> None:
    own_id = str(settings.device_id)
    chosen = None
    for record in _parse_records(records):
        if record["peer_id"] == own_id and record["op"] == "remove":
            chosen = record
            break
    if chosen is None:
        return

    def mutate(state: dict) -> None:
        current = state["records"].get(own_id)
        if not _beats(chosen, current):
            return
        state["departed"] = True
        state["clock"] = max(int(state.get("clock") or 0), int(chosen["epoch"]))
        state["records"][own_id] = chosen
        for peer in list(_local_network.paired_peers(settings)):
            _local_network.forget_peer(settings, _peer_id(peer), publish_membership=False)
            _unlink_cert(settings, _peer_id(peer))

    _with_state(settings, mutate)


def peer_id_for_removed_fingerprint(settings: Any, fingerprint: str) -> str:
    """Return a removed member whose pinned certificate matches ``fingerprint``."""
    expected = str(fingerprint or "").strip().lower()
    if not expected:
        return ""

    def read(state: dict) -> str:
        for peer_id, record in state["records"].items():
            if record.get("op") != "remove":
                continue
            card = record.get("card") if isinstance(record.get("card"), dict) else {}
            if str(card.get("certificate_fingerprint") or "").strip().lower() == expected:
                return str(peer_id)
        return ""

    return _with_state(settings, read)


def record_local_removal(settings: Any, peer_id: str, previous: Optional[dict] = None) -> None:
    normalized = str(peer_id or "").strip()
    if not normalized:
        return
    card = _card_from_peer(settings, dict(previous or {}))
    card.setdefault("drone_id", normalized)

    def mutate(state: dict) -> None:
        _store(
            state,
            {
                "peer_id": normalized,
                "op": "remove",
                "epoch": _mint(state),
                "origin": str(settings.device_id),
                "card": card,
            },
        )
        _mark_dirty(state)

    _with_state(settings, mutate)


def _post_to_peer(settings: Any, peer: dict, *, hop: int) -> dict:
    payload = membership_view(settings)
    payload["status"] = "sync"
    payload["introducer_id"] = str(settings.device_id)
    payload["hop"] = int(hop)
    poster = _poster or _default_poster
    response = poster(settings, peer, payload)
    return response if isinstance(response, dict) else {}


def _default_poster(settings: Any, peer: dict, payload: dict) -> dict:
    try:
        from .peer_connectivity import _peer_post_json_for_peer
    except ImportError:  # pragma: no cover
        from transfer.peer_connectivity import _peer_post_json_for_peer  # type: ignore
    body, _address = _peer_post_json_for_peer(
        peer,
        "/v1/api/peer/membership",
        payload,
        settings,
        peer_id=_peer_id(peer),
        timeout=3,
        config={"network_mode": "local_network"},
    )
    return body


def _with_state(settings: Any, fn: Callable[[dict], Any]) -> Any:
    with _lock_for(settings):
        state = _load(settings)
        _reconcile(settings, state)
        result = fn(state)
        _save(settings, state)
        return result


def _reconcile(settings: Any, state: dict) -> None:
    """Make the paired-peer list match stored adds and removals."""
    if state.get("departed"):
        for peer in list(_local_network.paired_peers(settings)):
            _local_network.forget_peer(settings, _peer_id(peer), publish_membership=False)
        return
    for peer_id, record in list(state["records"].items()):
        if peer_id == str(settings.device_id):
            continue
        if record.get("op") == "remove" and _local_network.get_paired_peer(settings, peer_id):
            _local_network.forget_peer(settings, peer_id, publish_membership=False)
        elif record.get("op") == "add" and _local_network.get_paired_peer(settings, peer_id) is None:
            _apply_add(settings, state, {"peer_id": peer_id, **record})


def _public_records(settings: Any, state: dict) -> dict[str, dict]:
    records = dict(state["records"])
    if state.get("departed"):
        return records
    for peer in _local_network.paired_peers(settings):
        peer_id = _peer_id(peer)
        if not peer_id or peer_id in records:
            continue
        card = _card_from_peer(settings, peer)
        if "BEGIN CERTIFICATE" not in str(card.get("certificate_pem") or ""):
            continue
        records[peer_id] = {"op": "add", "epoch": 0, "origin": str(settings.device_id), "card": card}
    return records


def _load(settings: Any) -> dict:
    payload = load_payload(_db(settings), MEMBERSHIP_NAMESPACE, {})
    if not isinstance(payload, dict):
        payload = {}
    records = payload.get("records") if isinstance(payload.get("records"), dict) else {}
    clean: dict[str, dict] = {}
    for peer_id, record in records.items():
        if isinstance(record, dict) and record.get("op") in {"add", "remove"}:
            try:
                epoch = int(record.get("epoch") or 0)
            except (TypeError, ValueError):
                continue
            clean[str(peer_id)] = {
                "op": record.get("op"),
                "epoch": epoch,
                "origin": str(record.get("origin") or ""),
                "card": record.get("card") if isinstance(record.get("card"), dict) else {},
            }
    try:
        clock = int(payload.get("clock") or 0)
    except (TypeError, ValueError):
        clock = 0
    try:
        dirty_gen = int(payload.get("dirty_gen") or 0)
    except (TypeError, ValueError):
        dirty_gen = 0
    try:
        forwarded_gen = int(payload.get("forwarded_gen") or 0)
    except (TypeError, ValueError):
        forwarded_gen = 0
    return {
        "clock": max(0, clock),
        "dirty_gen": max(0, dirty_gen),
        "forwarded_gen": max(0, forwarded_gen),
        "departed": bool(payload.get("departed")),
        "records": clean,
    }


def _save(settings: Any, state: dict) -> None:
    save_payload(_db(settings), MEMBERSHIP_NAMESPACE, state)


def _store(state: dict, record: dict) -> None:
    state["records"][record["peer_id"]] = {
        "op": record["op"],
        "epoch": int(record["epoch"]),
        "origin": str(record.get("origin") or ""),
        "card": record.get("card") if isinstance(record.get("card"), dict) else {},
    }


def _mint(state: dict) -> int:
    clock = int(state.get("clock") or 0) + 1
    state["clock"] = clock
    return clock


def _mark_dirty(state: dict) -> None:
    state["dirty_gen"] = int(state.get("dirty_gen") or 0) + 1


def _beats(remote: dict, local: Optional[dict]) -> bool:
    if local is None:
        return True
    remote_epoch = int(remote.get("epoch") or 0)
    local_epoch = int(local.get("epoch") or 0)
    if remote_epoch > local_epoch:
        return True
    return remote_epoch == local_epoch and remote.get("op") == "remove" and local.get("op") == "add"


def _parse_records(records: Any) -> list[dict]:
    if not isinstance(records, list):
        return []
    parsed = []
    for raw in records[:_MAX_RECORDS]:
        if not isinstance(raw, dict):
            continue
        peer_id = str(raw.get("peer_id") or "").strip()
        op = str(raw.get("op") or "").strip()
        if not peer_id or op not in {"add", "remove"} or len(peer_id) > 128:
            continue
        epoch = raw.get("epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0 or epoch > 1_000_000_000:
            continue
        card = raw.get("card") if isinstance(raw.get("card"), dict) else {}
        parsed.append(
            {
                "peer_id": peer_id,
                "op": op,
                "epoch": epoch,
                "origin": str(raw.get("origin") or "")[:128],
                "card": {key: card.get(key) for key in _CARD_FIELDS if key in card},
            }
        )
    return parsed


def _public_record(peer_id: str, record: dict) -> dict:
    card = record.get("card") if isinstance(record.get("card"), dict) else {}
    return {
        "peer_id": peer_id,
        "op": record.get("op"),
        "epoch": int(record.get("epoch") or 0),
        "origin": str(record.get("origin") or ""),
        "card": {key: card.get(key) for key in _CARD_FIELDS if key in card},
    }


def _removed_response(settings: Any, tombstone: dict) -> dict:
    view = membership_view(settings)
    own = _public_record(tombstone["peer_id"], tombstone)
    return {"status": "removed", "clock": view["clock"], "records": [own]}


def _tombstone_for(settings: Any, peer_id: str) -> Optional[dict]:
    if not peer_id:
        return None

    def read(state: dict) -> Optional[dict]:
        record = state["records"].get(peer_id)
        if not record or record.get("op") != "remove":
            return None
        return {"peer_id": peer_id, **record}

    return _with_state(settings, read)


def _card_from_peer(settings: Any, peer: dict) -> dict:
    peer_id = _peer_id(peer)
    pem = _read_pem(settings, peer)
    fingerprint = str(peer.get("certificate_fingerprint") or "").strip().lower()
    if pem and not fingerprint:
        try:
            fingerprint = _certificate_pem_fingerprint(pem).lower()
        except Exception:
            fingerprint = ""
    card = {
        "name": str(peer.get("name") or peer_id),
        "hostname": str(peer.get("hostname") or ""),
        "reachable_url": str(peer.get("reachable_url") or ""),
        "advertised_reachable_url": str(peer.get("advertised_reachable_url") or ""),
        "scheme": str(peer.get("scheme") or "https"),
        "api_port": _port(peer.get("api_port"), 443),
        "peer_mtls_port": _port(peer.get("peer_mtls_port"), _port(peer.get("api_port"), 443)),
        "tailnet_ip": str(peer.get("tailnet_ip") or ""),
        "source_ip": str(peer.get("source_ip") or ""),
        "certificate_fingerprint": fingerprint,
    }
    if pem:
        card["certificate_pem"] = pem
    return card


def _read_pem(settings: Any, peer: dict) -> str:
    inline = str(peer.get("certificate_pem") or "")
    if "BEGIN CERTIFICATE" in inline:
        return inline
    candidates = []
    raw_path = str(peer.get("certificate_path") or "").strip()
    if raw_path:
        candidates.append(Path(raw_path))
    peer_id = _peer_id(peer)
    if peer_id:
        try:
            from .peer_connectivity import _local_peer_cert_cache_path
        except ImportError:  # pragma: no cover
            from transfer.peer_connectivity import _local_peer_cert_cache_path  # type: ignore
        candidates.append(_local_peer_cert_cache_path(settings, peer_id))
    for path in candidates:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "BEGIN CERTIFICATE" in text:
            return text
    return ""


def _activate_certificate(cert_path: Path) -> None:
    try:
        from ..web.server_tls import activate_peer_certificate
    except ImportError:  # pragma: no cover
        from web.server_tls import activate_peer_certificate  # type: ignore
    activate_peer_certificate(cert_path)


def _unlink_cert(settings: Any, peer_id: str) -> None:
    try:
        from .peer_connectivity import _local_peer_cert_cache_path
    except ImportError:  # pragma: no cover
        from transfer.peer_connectivity import _local_peer_cert_cache_path  # type: ignore
    try:
        _local_peer_cert_cache_path(settings, peer_id).unlink(missing_ok=True)
    except OSError:
        pass


def _departed(settings: Any) -> bool:
    return bool(_with_state(settings, lambda state: state.get("departed")))


def _is_dirty(settings: Any) -> bool:
    def read(state: dict) -> bool:
        return int(state.get("dirty_gen") or 0) != int(state.get("forwarded_gen") or 0)

    return _with_state(settings, read)


def _dirty_gen(settings: Any) -> int:
    return int(_with_state(settings, lambda state: int(state.get("dirty_gen") or 0)))


def _mark_forwarded(settings: Any, captured: int) -> None:
    def mutate(state: dict) -> None:
        if int(state.get("dirty_gen") or 0) == int(captured):
            state["forwarded_gen"] = int(captured)

    _with_state(settings, mutate)


def _peer_id(peer: dict) -> str:
    return str((peer or {}).get("drone_id") or (peer or {}).get("device_id") or "").strip()


def _port(value: Any, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    if port < 1 or port > 65535:
        return default
    return port


def _hop(payload: dict) -> int:
    try:
        hop = int(payload.get("hop") or 0)
    except (TypeError, ValueError):
        return 0
    if hop < 0:
        return 0
    return min(hop, MAX_FORWARD_HOP)


def _db(settings: Any) -> Path:
    return database_path(settings.userdata_root)


def _lock_for(settings: Any) -> threading.Lock:
    key = str(_db(settings))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock
