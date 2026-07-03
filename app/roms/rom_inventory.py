"""ROM/BIOS inventory identity: cache-entry keys + whole-set fingerprints.

Extracted from ``drone_api.py``. Pure(ish) helpers that derive stable cache-entry
keys and the SHA-256 "inventory fingerprint" the heartbeat echoes so the drone can
tell when its uploaded inventory drifts from Overmind's. Data-only aside from
reading the persisted cache-state fingerprint from the SQLite cache.
"""

import hashlib
import json
import os
from typing import Iterable, List, Optional

try:
    from ..common.settings import Settings
    from ..storage.rom_metadata_store import _read_rom_metadata_cache_state
    from .gamelist import _normalize_gamelist_rom_path
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage.rom_metadata_store import _read_rom_metadata_cache_state  # type: ignore
    from roms.gamelist import _normalize_gamelist_rom_path  # type: ignore


def _rom_cache_entry_key(system: str, relative_path: str) -> str:
    normalized_path = _normalize_gamelist_rom_path(str(relative_path or ""))
    return f"{system.strip().lower()}:{normalized_path}"


def _bios_cache_entry_key(relative_path: str) -> str:
    return _normalize_gamelist_rom_path(str(relative_path or "")).lower()


def _artwork_cache_entry_key(system: str, rom_path: str) -> str:
    return f"{str(system or '').strip().lower()}:{_normalize_gamelist_rom_path(str(rom_path or '')).lower()}"


ROM_INVENTORY_FINGERPRINT_ALGORITHM = "rom-inventory-sha256-v1"


def _normalize_rom_inventory_path(value: object) -> str:
    return str(value or "").replace("\\", "/").strip().lstrip("./").lower()


def _rom_inventory_fingerprint(roms: Iterable[dict]) -> str:
    rows = []
    for row in roms or []:
        if not isinstance(row, dict):
            continue
        system = str(row.get("system") or row.get("system_name") or "").strip().lower()
        path = _normalize_rom_inventory_path(
            row.get("file_path")
            or row.get("relative_path")
            or row.get("rom_path")
            or row.get("rom_file")
            or row.get("rom_name")
            or row.get("name")
        )
        if not system or not path:
            continue
        entry_type = str(row.get("entry_type") or "file").strip().lower()
        fingerprint_value = str(row.get("rom_fingerprint") or row.get("fingerprint") or row.get("hash") or "").strip().lower()
        file_size = row.get("file_size") if row.get("file_size") is not None else row.get("byte_count")
        size_value = str(int(file_size)) if isinstance(file_size, (int, float)) else str(file_size or "").strip()
        rows.append("\t".join((system, path, entry_type, fingerprint_value, size_value)))
    digest = hashlib.sha256()
    for value in sorted(rows):
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _rom_inventory_fingerprint_from_cache_state(settings: Settings) -> Optional[str]:
    try:
        state = _read_rom_metadata_cache_state(settings, "rom_inventory_fingerprint")
    except Exception:
        return None
    value = str(state.get("rom_inventory_fingerprint") or "").strip()
    return value or None


# Wholistic per-asset-class "thumbprints" round-tripped with Overmind so the Drone
# (not Overmind) decides when a re-sync is needed: Overmind echoes the thumbprints it
# last stored, the Drone compares them against what it currently holds on disk, and only
# pushes when they differ. The romset thumbprint reuses the ROM inventory fingerprint;
# BIOS gets its own so the two asset classes can drift independently.
BIOS_INVENTORY_FINGERPRINT_ALGORITHM = "bios-inventory-sha256-v1"


def _bios_inventory_fingerprint(bios: Iterable[dict]) -> str:
    rows = []
    for row in bios or []:
        if not isinstance(row, dict):
            continue
        path = _normalize_rom_inventory_path(
            row.get("relative_path")
            or row.get("file_path")
            or row.get("path")
            or row.get("name")
            or row.get("bios_name")
        )
        if not path:
            continue
        md5_value = str(row.get("bios_md5") or row.get("md5") or row.get("fingerprint") or "").strip().lower()
        file_size = row.get("file_size") if row.get("file_size") is not None else row.get("byte_count")
        size_value = str(int(file_size)) if isinstance(file_size, (int, float)) else str(file_size or "").strip()
        rows.append("\t".join((path, md5_value, size_value)))
    digest = hashlib.sha256()
    for value in sorted(rows):
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


ROM_METADATA_UPLOAD_CHUNK_SIZE = max(1, int(os.environ.get("ROM_METADATA_UPLOAD_CHUNK_SIZE", "250")))


def _rom_metadata_inventory_id(settings: Settings, snapshot: dict) -> str:
    counts = (
        len(snapshot.get("roms") if isinstance(snapshot.get("roms"), list) else []),
        len(snapshot.get("bios") if isinstance(snapshot.get("bios"), list) else []),
        len(snapshot.get("artwork") if isinstance(snapshot.get("artwork"), list) else []),
    )
    return f"{settings.overmind_device_id}:{snapshot.get('collected_at') or ''}:{counts[0]}:{counts[1]}:{counts[2]}"


def _chunk_rom_metadata_inventory(
    settings: Settings,
    snapshot: dict,
    chunk_size: Optional[int] = None,
    *,
    replace_all: bool = False,
) -> List[dict]:
    chunk_size = max(1, int(chunk_size or ROM_METADATA_UPLOAD_CHUNK_SIZE))
    roms = _wire_asset_rows(snapshot.get("roms") if isinstance(snapshot.get("roms"), list) else [])
    bios = _wire_asset_rows(snapshot.get("bios") if isinstance(snapshot.get("bios"), list) else [])
    artwork = _wire_asset_rows(snapshot.get("artwork") if isinstance(snapshot.get("artwork"), list) else [])
    rows = [("roms", row) for row in roms] + [("bios", row) for row in bios] + [("artwork", row) for row in artwork]
    base = {
        "device_id": settings.overmind_device_id,
        "type": snapshot.get("type") or "asset_metadata",
        "collected_at": snapshot.get("collected_at"),
        "roms_root": snapshot.get("roms_root"),
        "bios_root": snapshot.get("bios_root"),
        "rom_inventory_fingerprint": snapshot.get("rom_inventory_fingerprint") or _rom_inventory_fingerprint(roms),
        "rom_inventory_fingerprint_algorithm": snapshot.get("rom_inventory_fingerprint_algorithm") or ROM_INVENTORY_FINGERPRINT_ALGORITHM,
        "romset_files_thumbprint": snapshot.get("romset_files_thumbprint") or snapshot.get("rom_inventory_fingerprint") or _rom_inventory_fingerprint(roms),
        "bios_files_thumbprint": snapshot.get("bios_files_thumbprint") or _bios_inventory_fingerprint(bios),
        "systems": snapshot.get("systems") if isinstance(snapshot.get("systems"), list) else [],
        "gamelists": snapshot.get("gamelists") if isinstance(snapshot.get("gamelists"), list) else [],
        "cache": snapshot.get("cache") if isinstance(snapshot.get("cache"), dict) else {},
        "replace_all": bool(replace_all),
    }
    if len(rows) <= chunk_size:
        return [{**base, "update_mode": "inventory", "roms": roms, "bios": bios, "artwork": artwork}]

    chunks = []
    total = (len(rows) + chunk_size - 1) // chunk_size
    inventory_id = _rom_metadata_inventory_id(settings, snapshot)
    counts = {"roms": len(roms), "bios": len(bios), "artwork": len(artwork)}
    for index in range(total):
        chunk_rows = rows[index * chunk_size:(index + 1) * chunk_size]
        payload = {
            **base,
            "update_mode": "inventory_chunk",
            "inventory_id": inventory_id,
            "chunk_index": index,
            "chunk_total": total,
            "inventory_complete": index == total - 1,
            "inventory_counts": counts,
            "roms": [],
            "bios": [],
            "artwork": [],
        }
        for asset_type, row in chunk_rows:
            payload[asset_type].append(row)
        chunks.append(payload)
    return chunks


def _wire_asset_rows(rows: list) -> list:
    return [
        {key: value for key, value in row.items() if key != "absolute_path"}
        for row in rows
        if isinstance(row, dict)
    ]


def _chunk_rom_metadata_delta(settings: Settings, snapshot: dict, changes: dict, chunk_size: Optional[int] = None) -> List[dict]:
    chunk_size = max(1, int(chunk_size or ROM_METADATA_UPLOAD_CHUNK_SIZE))
    deleted = changes.get("deleted") if isinstance(changes.get("deleted"), dict) else {}
    rows = (
        [("roms", "upsert", row) for row in _wire_asset_rows(changes.get("roms") or [])]
        + [("bios", "upsert", row) for row in _wire_asset_rows(changes.get("bios") or [])]
        + [("artwork", "upsert", row) for row in _wire_asset_rows(changes.get("artwork") or [])]
        + [("roms", "delete", row) for row in _wire_asset_rows(deleted.get("roms") or [])]
        + [("bios", "delete", row) for row in _wire_asset_rows(deleted.get("bios") or [])]
        + [("artwork", "delete", row) for row in _wire_asset_rows(deleted.get("artwork") or [])]
    )
    if not rows:
        return []
    base = {
        "device_id": settings.overmind_device_id,
        "type": snapshot.get("type") or "asset_metadata",
        "update_mode": "inventory_delta",
        "collected_at": snapshot.get("collected_at"),
        "rom_inventory_fingerprint": snapshot.get("rom_inventory_fingerprint") or _rom_inventory_fingerprint(snapshot.get("roms") if isinstance(snapshot.get("roms"), list) else []),
        "rom_inventory_fingerprint_algorithm": snapshot.get("rom_inventory_fingerprint_algorithm") or ROM_INVENTORY_FINGERPRINT_ALGORITHM,
        "romset_files_thumbprint": snapshot.get("romset_files_thumbprint") or snapshot.get("rom_inventory_fingerprint") or _rom_inventory_fingerprint(snapshot.get("roms") if isinstance(snapshot.get("roms"), list) else []),
        "bios_files_thumbprint": snapshot.get("bios_files_thumbprint") or _bios_inventory_fingerprint(snapshot.get("bios") if isinstance(snapshot.get("bios"), list) else []),
        "systems": snapshot.get("systems") if isinstance(snapshot.get("systems"), list) else [],
    }
    payloads = []
    total = (len(rows) + chunk_size - 1) // chunk_size
    for index, start in enumerate(range(0, len(rows), chunk_size)):
        payload = {
            **base,
            "delta_index": index,
            "delta_total": total,
            "inventory_complete": index == total - 1,
            "roms": [],
            "bios": [],
            "artwork": [],
            "deleted": {"roms": [], "bios": [], "artwork": []},
        }
        for asset_type, operation, row in rows[start:start + chunk_size]:
            if operation == "delete":
                payload["deleted"][asset_type].append(row)
            else:
                payload[asset_type].append(row)
        payloads.append(payload)
    return payloads


def _json_payload_size_bytes(payload: dict) -> int:
    try:
        return len(json.dumps(payload).encode("utf-8"))
    except (TypeError, ValueError):
        return 0
