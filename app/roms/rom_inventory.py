"""ROM/BIOS inventory identity: cache-entry keys + whole-set fingerprints.

Extracted from ``drone_api.py``. Pure(ish) helpers that derive stable cache-entry
keys and the SHA-256 "inventory fingerprint" used to tell when the cached ROM/BIOS
set has changed since the last completed scan. Data-only aside from reading the
persisted cache-state fingerprint from the SQLite cache.
"""

import hashlib
from typing import Iterable, Optional

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


# Wholistic per-asset-class "thumbprints", checked against what's on disk so a scan
# only marks the cache dirty when something actually changed. The romset thumbprint
# reuses the ROM inventory fingerprint; BIOS gets its own so the two asset classes
# can drift independently.
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


def _wire_rom_rows(rows: list) -> list:
    """Project each cached ROM row to its slim wire shape: gamelist id, display name,
    system, file size, and sampled fingerprint. gamelist.xml is the source of truth,
    so the ROM path, artwork, and other local-only fields are omitted -- a peer
    identifies a game by (system, gamelist_id) + fingerprint and resolves the actual
    file from its own gamelist at transfer time."""
    slim = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        fingerprint = row.get("rom_fingerprint") or row.get("fingerprint")
        wire = {
            "gamelist_game_id": str(row.get("gamelist_game_id") or ""),
            "system_name": str(row.get("system") or row.get("system_name") or ""),
            "name": str(row.get("name") or row.get("rom_name") or row.get("title") or ""),
            "file_size": int(row.get("file_size") or row.get("byte_count") or row.get("size") or 0),
            # "folder" covers both true directory entries and folder-unit ROMs (a marker
            # file whose per-game folder is the transfer unit); file_size is then the
            # folder's total bytes, so the real download size is shown.
            "entry_type": str(row.get("entry_type") or "file"),
        }
        # Omit the fingerprint until it is computed (folders and freshly-scanned files
        # carry no fingerprint yet; it arrives later via a hash patch).
        if fingerprint:
            wire["rom_fingerprint"] = str(fingerprint)
        slim.append(wire)
    return slim
