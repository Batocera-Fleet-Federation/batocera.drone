"""SQLite persistence for Drone ROM metadata cache rows."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

try:
    from .state_store import database_path as _state_database_path
    from .state_store import open_database as _open_state_database
except ImportError:
    from state_store import database_path as _state_database_path  # type: ignore
    from state_store import open_database as _open_state_database  # type: ignore


ROM_METADATA_CACHE_VERSION = 2


def _rom_metadata_cache_path(settings: Any) -> Path:
    return _state_database_path(settings.userdata_root)


def _legacy_rom_metadata_cache_path(settings: Any) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / "rom_metadata_cache.json").resolve()


def _empty_rom_metadata_cache() -> dict:
    return {
        "schema_version": ROM_METADATA_CACHE_VERSION,
        "last_full_scan_at": None,
        "last_successful_upload_at": None,
        "entries": {},
        "bios_entries": {},
        "artwork_entries": {},
        "systems": [],
        "gamelists": [],
        "dirty": True,
    }


def _read_json_file(path: Path, fallback: Any) -> Any:
    try:
        if path.exists() and path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    return fallback


def _format_store_error(error: BaseException) -> str:
    message = str(error).strip()
    if message:
        return f"{error.__class__.__name__}: {message}"
    return repr(error)


def _open_rom_metadata_cache(settings: Any):
    connection = _open_state_database(_rom_metadata_cache_path(settings))
    connection.execute(
        "CREATE TABLE IF NOT EXISTS cache_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS cache_entries (asset_type TEXT NOT NULL, entry_key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (asset_type, entry_key))"
    )
    return connection


def _persist_rom_metadata_cache(
    settings: Any,
    cache: dict,
    *,
    rom_updates: Optional[dict] = None,
    bios_updates: Optional[dict] = None,
    artwork_updates: Optional[dict] = None,
    rom_deletes: Optional[Iterable[str]] = None,
    bios_deletes: Optional[Iterable[str]] = None,
    artwork_deletes: Optional[Iterable[str]] = None,
) -> None:
    """Persist only changed metadata rows plus compact scan state."""
    state = {
        key: value
        for key, value in cache.items()
        if key not in {"entries", "bios_entries", "artwork_entries"}
    }
    updates = (
        ("rom", rom_updates or {}),
        ("bios", bios_updates or {}),
        ("artwork", artwork_updates or {}),
    )
    deletions = (
        ("rom", rom_deletes or []),
        ("bios", bios_deletes or []),
        ("artwork", artwork_deletes or []),
    )
    with _open_rom_metadata_cache(settings) as connection:
        connection.executemany(
            "INSERT INTO cache_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(key, json.dumps(value, sort_keys=True, default=str)) for key, value in state.items()],
        )
        for asset_type, rows in updates:
            connection.executemany(
                "INSERT INTO cache_entries (asset_type, entry_key, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(asset_type, entry_key) DO UPDATE SET payload=excluded.payload",
                [
                    (asset_type, key, json.dumps(value, sort_keys=True, default=str))
                    for key, value in rows.items()
                ],
            )
        for asset_type, keys in deletions:
            connection.executemany(
                "DELETE FROM cache_entries WHERE asset_type = ? AND entry_key = ?",
                [(asset_type, key) for key in keys],
            )


def _update_rom_metadata_cache_state(settings: Any, **values: Any) -> None:
    """Update compact scan/upload state without reading all cached asset rows."""
    if not values:
        return
    with _open_rom_metadata_cache(settings) as connection:
        connection.executemany(
            "INSERT INTO cache_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(key, json.dumps(value, sort_keys=True, default=str)) for key, value in values.items()],
        )


def _read_sqlite_rom_metadata_cache(settings: Any) -> dict:
    with _open_rom_metadata_cache(settings) as connection:
        state = {
            key: json.loads(value)
            for key, value in connection.execute("SELECT key, value FROM cache_state")
        }
        if state.get("schema_version") != ROM_METADATA_CACHE_VERSION:
            raise ValueError("cache schema mismatch")
        cache = {**_empty_rom_metadata_cache(), **state}
        entries = {"rom": {}, "bios": {}, "artwork": {}}
        for asset_type, key, payload in connection.execute(
            "SELECT asset_type, entry_key, payload FROM cache_entries"
        ):
            if asset_type in entries:
                entries[asset_type][key] = json.loads(payload)
        cache["entries"] = entries["rom"]
        cache["bios_entries"] = entries["bios"]
        cache["artwork_entries"] = entries["artwork"]
        return cache


def _load_rom_metadata_cache(settings: Any) -> Tuple[dict, bool]:
    path = _rom_metadata_cache_path(settings)
    legacy_path = _legacy_rom_metadata_cache_path(settings)
    try:
        if path.exists():
            try:
                return _read_sqlite_rom_metadata_cache(settings), False
            except ValueError:
                # The shared state database may exist before metadata has been collected.
                with _open_rom_metadata_cache(settings) as connection:
                    connection.execute("DELETE FROM cache_state")
                    connection.execute("DELETE FROM cache_entries")
        legacy = _read_json_file(legacy_path, None)
        if isinstance(legacy, dict) and isinstance(legacy.get("entries"), dict):
            legacy["schema_version"] = ROM_METADATA_CACHE_VERSION
            legacy.setdefault("bios_entries", {})
            legacy.setdefault("artwork_entries", {})
            legacy.setdefault("systems", [])
            legacy.setdefault("gamelists", [])
            legacy.setdefault("dirty", True)
            _persist_rom_metadata_cache(
                settings,
                legacy,
                rom_updates=legacy["entries"],
                bios_updates=legacy["bios_entries"],
                artwork_updates=legacy["artwork_entries"],
            )
            print(f"Asset metadata cache migrated to incremental store: {path}", file=sys.stdout, flush=True)
            return legacy, False
        return _empty_rom_metadata_cache(), True
    except Exception as error:
        print(f"Asset metadata cache rebuild required: {path} ({_format_store_error(error)})", file=sys.stderr, flush=True)
        if path.exists():
            try:
                path.replace(path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex}"))
            except Exception:
                pass
        return _empty_rom_metadata_cache(), True
