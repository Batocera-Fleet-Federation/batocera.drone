"""ROM-metadata cache state: snapshot build, upload-clean marking, status, activity guard.

Extracted from ``drone_api.py``. Builds an upload snapshot from the SQLite ROM-metadata
cache (optionally rehydrating gamelist references), marks the cache clean after a
successful upload, reports cache status, and guards the poll with the shared
``_ROM_METADATA_ACTIVE`` Event (in ``common.runtime_state``).
"""

from datetime import datetime, timezone
from typing import Optional

try:
    from ..common.logging_setup import _drone_log
    from ..common.runtime_state import _ROM_METADATA_ACTIVE, _ROM_METADATA_LOCK
    from ..common.settings import Settings
    from ..storage.rom_metadata_store import (
        ROM_METADATA_CACHE_VERSION,
        _clear_pending_rom_metadata_changes,
        _load_rom_metadata_cache,
        _read_pending_rom_metadata_changes,
        _rom_metadata_cache_path,
        _update_rom_metadata_cache_state,
    )
    from .gamelist import _gamelist_metadata_for_reference
    from .rom_inventory import BIOS_INVENTORY_FINGERPRINT_ALGORITHM, ROM_INVENTORY_FINGERPRINT_ALGORITHM, _bios_inventory_fingerprint, _rom_inventory_fingerprint
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.logging_setup import _drone_log  # type: ignore
    from common.runtime_state import _ROM_METADATA_ACTIVE, _ROM_METADATA_LOCK  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage.rom_metadata_store import (  # type: ignore
        ROM_METADATA_CACHE_VERSION,
        _clear_pending_rom_metadata_changes,
        _load_rom_metadata_cache,
        _read_pending_rom_metadata_changes,
        _rom_metadata_cache_path,
        _update_rom_metadata_cache_state,
    )
    from roms.gamelist import _gamelist_metadata_for_reference  # type: ignore
    from roms.rom_inventory import BIOS_INVENTORY_FINGERPRINT_ALGORITHM, ROM_INVENTORY_FINGERPRINT_ALGORITHM, _bios_inventory_fingerprint, _rom_inventory_fingerprint  # type: ignore


def _build_rom_metadata_snapshot_from_cache(settings: Settings, cache: dict, rehydrate_gamelist: bool = False) -> dict:
    # ARTWORK_FIELDS stays in drone_api; lazy-import to avoid a cycle.
    try:
        from ..drone_api import ARTWORK_FIELDS
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import ARTWORK_FIELDS  # type: ignore
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    roms = []
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        row = {k: v for k, v in entry.items() if k != "absolute_path"}
        gamelist_details = (
            _gamelist_metadata_for_reference(
                str(row.get("gamelist_path") or ""),
                str(row.get("gamelist_game_id") or row.get("file_path") or row.get("rom_path") or ""),
            )
            if rehydrate_gamelist
            else {}
        )
        row["gamelist"] = gamelist_details
        row["existing"] = {field: str(gamelist_details.get(field) or "") for field in ARTWORK_FIELDS}
        row["has_gamelist_entry"] = bool(row.get("gamelist_path"))
        row["metadata_source"] = "gamelist.xml" if row.get("gamelist_path") else row.get("metadata_source") or "filesystem"
        title = str(gamelist_details.get("name") or "").strip()
        if title:
            row["name"] = title
            row["rom_name"] = title
            row["title"] = title
        roms.append(row)
    roms.sort(key=lambda row: (str(row.get("system") or ""), str(row.get("file_path") or "")))
    bios_entries = cache.get("bios_entries") if isinstance(cache.get("bios_entries"), dict) else {}
    bios = []
    for entry in bios_entries.values():
        if not isinstance(entry, dict):
            continue
        bios.append({k: v for k, v in entry.items() if k != "absolute_path"})
    bios.sort(key=lambda row: str(row.get("file_path") or row.get("path") or ""))
    artwork_entries = cache.get("artwork_entries") if isinstance(cache.get("artwork_entries"), dict) else {}
    artwork = []
    for entry in artwork_entries.values():
        if not isinstance(entry, dict):
            continue
        artwork.append(dict(entry))
    artwork.sort(key=lambda row: (str(row.get("system") or ""), str(row.get("rom_path") or "")))
    fingerprint = _rom_inventory_fingerprint(roms)
    bios_thumbprint = _bios_inventory_fingerprint(bios)
    return {
        "type": "asset_metadata",
        "collected_at": cache.get("last_full_scan_at") or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "roms_root": str(settings.roms_root),
        "bios_root": str(settings.bios_root),
        "rom_inventory_fingerprint": fingerprint,
        "rom_inventory_fingerprint_algorithm": ROM_INVENTORY_FINGERPRINT_ALGORITHM,
        "romset_files_thumbprint": fingerprint,
        "bios_files_thumbprint": bios_thumbprint,
        "bios_inventory_fingerprint_algorithm": BIOS_INVENTORY_FINGERPRINT_ALGORITHM,
        "systems": cache.get("systems") if isinstance(cache.get("systems"), list) else [],
        "roms": roms,
        "bios": bios,
        "artwork": artwork,
        "gamelists": cache.get("gamelists") if isinstance(cache.get("gamelists"), list) else [],
        "cache": {"schema_version": ROM_METADATA_CACHE_VERSION},
    }


def _mark_rom_metadata_upload_clean(
    settings: Settings,
    fingerprint: Optional[str] = None,
    bios_thumbprint: Optional[str] = None,
) -> None:
    state = {
        "last_successful_upload_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "dirty": False,
        "full_refresh_pending": False,
    }
    if fingerprint:
        state["rom_inventory_fingerprint"] = fingerprint
        state["rom_inventory_fingerprint_algorithm"] = ROM_INVENTORY_FINGERPRINT_ALGORITHM
        state["romset_files_thumbprint"] = fingerprint
    if bios_thumbprint is not None:
        state["bios_files_thumbprint"] = bios_thumbprint
        state["bios_inventory_fingerprint_algorithm"] = BIOS_INVENTORY_FINGERPRINT_ALGORITHM
    _clear_pending_rom_metadata_changes(settings)
    _update_rom_metadata_cache_state(settings, **state)


def _rom_metadata_cache_status(settings: Settings) -> dict:
    # Watcher config + the watcher singleton stay in drone_api; lazy-import to avoid a cycle.
    try:
        from ..drone_api import (
            ROM_METADATA_HASH_ROMS_ENABLED,
            ROM_METADATA_INITIAL_DELAY_SECONDS,
            ROM_METADATA_WATCH_ENABLED,
            _ROM_METADATA_WATCHER,
        )
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import (  # type: ignore
            ROM_METADATA_HASH_ROMS_ENABLED,
            ROM_METADATA_INITIAL_DELAY_SECONDS,
            ROM_METADATA_WATCH_ENABLED,
            _ROM_METADATA_WATCHER,
        )
    cache, rebuilt = _load_rom_metadata_cache(settings)
    changes = _read_pending_rom_metadata_changes(settings)
    roms = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    bios = cache.get("bios_entries") if isinstance(cache.get("bios_entries"), dict) else {}
    artwork = cache.get("artwork_entries") if isinstance(cache.get("artwork_entries"), dict) else {}
    deleted = changes.get("deleted") if isinstance(changes.get("deleted"), dict) else {}
    pending = {
        "roms": len(changes.get("roms") if isinstance(changes.get("roms"), list) else []),
        "bios": len(changes.get("bios") if isinstance(changes.get("bios"), list) else []),
        "artwork": len(changes.get("artwork") if isinstance(changes.get("artwork"), list) else []),
        "deleted_roms": len(deleted.get("roms") if isinstance(deleted.get("roms"), list) else []),
        "deleted_bios": len(deleted.get("bios") if isinstance(deleted.get("bios"), list) else []),
        "deleted_artwork": len(deleted.get("artwork") if isinstance(deleted.get("artwork"), list) else []),
    }
    pending["total"] = sum(pending.values())
    complete = bool(cache.get("last_full_scan_at")) and not bool(cache.get("scan_in_progress"))
    uploaded = bool(cache.get("last_successful_upload_at"))
    cached_assets = len(roms) + len(bios) + len(artwork)
    return {
        "path": str(_rom_metadata_cache_path(settings)),
        "schema_version": cache.get("schema_version"),
        "rebuilt": rebuilt,
        "active": _ROM_METADATA_ACTIVE.is_set(),
        "poller_enabled": settings.rom_metadata_poll_seconds != 0,
        "poll_seconds": settings.rom_metadata_poll_seconds,
        "watch_enabled": ROM_METADATA_WATCH_ENABLED,
        "watch_active": _ROM_METADATA_WATCHER is not None,
        "rom_hashing_enabled": ROM_METADATA_HASH_ROMS_ENABLED,
        "initial_delay_seconds": ROM_METADATA_INITIAL_DELAY_SECONDS,
        "complete": complete,
        "uploaded": uploaded,
        "needs_upload": bool(cached_assets and (cache.get("dirty") or cache.get("full_refresh_pending") or pending["total"])),
        "dirty": bool(cache.get("dirty")),
        "full_refresh_pending": bool(cache.get("full_refresh_pending")),
        "scan_in_progress": bool(cache.get("scan_in_progress")),
        "last_full_scan_at": cache.get("last_full_scan_at"),
        "last_successful_upload_at": cache.get("last_successful_upload_at"),
        "scan_checkpoint_at": cache.get("scan_checkpoint_at"),
        "counts": {
            "systems": len(cache.get("systems") if isinstance(cache.get("systems"), list) else []),
            "roms": len(roms),
            "bios": len(bios),
            "artwork": len(artwork),
            "total": cached_assets,
        },
        "pending_changes": pending,
    }


def _begin_rom_metadata_activity(reason: str) -> bool:
    if not _ROM_METADATA_LOCK.acquire(blocking=False):
        _drone_log(f"Asset metadata {reason} skipped: metadata work already running")
        return False
    _ROM_METADATA_ACTIVE.set()
    return True


def _end_rom_metadata_activity() -> None:
    _ROM_METADATA_ACTIVE.clear()
    _ROM_METADATA_LOCK.release()
