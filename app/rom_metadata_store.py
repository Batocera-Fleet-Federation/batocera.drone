"""Relational SQLite persistence for Drone asset metadata."""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

try:
    from .state_store import database_path as _state_database_path
    from .state_store import open_database as _open_state_database
except ImportError:
    from state_store import database_path as _state_database_path  # type: ignore
    from state_store import open_database as _open_state_database  # type: ignore


ROM_METADATA_CACHE_VERSION = 4
_ROW_EXTRA_KEYS = {
    "system",
    "system_name",
    "rom_name",
    "name",
    "title",
    "rom_file",
    "filename",
    "relative_path",
    "rom_path",
    "file_path",
    "absolute_path",
    "byte_count",
    "size",
    "file_size",
    "modified_time",
    "mtime",
    "md5",
    "rom_md5",
    "bios_md5",
    "source",
    "metadata_source",
    "entry_type",
    "is_downloadable",
    "image_stem",
    "unique_id",
    "path",
    "gamelist",
    "existing",
    "has_gamelist_entry",
    "gamelist_path",
    "gamelist_game_id",
}


@dataclass(frozen=True)
class RomCacheRow:
    entry_key: str
    system: str
    file_path: str
    rom_name: str
    unique_id: str
    absolute_path: str
    file_size: int
    modified_time: int
    entry_type: str
    md5: Optional[str]
    gamelist_path: str
    gamelist_game_id: str
    is_downloadable: bool
    image_stem: str
    extra: dict

    @classmethod
    def from_payload(cls, entry_key: str, payload: dict) -> "RomCacheRow":
        system = str(payload.get("system") or payload.get("system_name") or "").strip()
        file_path = _normalize_path(payload.get("file_path") or payload.get("relative_path") or payload.get("rom_path") or payload.get("rom_file"))
        rom_name = str(payload.get("rom_name") or payload.get("name") or payload.get("title") or Path(file_path).stem).strip()
        gamelist_path = str(payload.get("gamelist_path") or "")
        gamelist_game_id = str(payload.get("gamelist_game_id") or file_path)
        if gamelist_path:
            rom_name = Path(file_path).stem
        return cls(
            entry_key=entry_key,
            system=system,
            file_path=file_path,
            rom_name=rom_name or file_path,
            unique_id=str(payload.get("unique_id") or ""),
            absolute_path=str(payload.get("absolute_path") or ""),
            file_size=_int(payload.get("file_size") or payload.get("byte_count") or payload.get("size")),
            modified_time=_int(payload.get("modified_time") or payload.get("mtime")),
            entry_type=str(payload.get("entry_type") or "file"),
            md5=_clean_optional(payload.get("rom_md5") or payload.get("md5")),
            gamelist_path=gamelist_path,
            gamelist_game_id=gamelist_game_id,
            is_downloadable=bool(payload.get("is_downloadable", True)),
            image_stem=str(payload.get("image_stem") or Path(file_path).stem),
            extra={},
        )

    def to_payload(self) -> dict:
        payload = {
            **self.extra,
            "system": self.system,
            "system_name": self.system,
            "rom_name": self.rom_name,
            "name": self.rom_name,
            "title": self.rom_name,
            "rom_file": Path(self.file_path).name,
            "filename": Path(self.file_path).name,
            "relative_path": self.file_path,
            "rom_path": self.file_path,
            "file_path": self.file_path,
            "absolute_path": self.absolute_path,
            "byte_count": self.file_size,
            "size": self.file_size,
            "file_size": self.file_size,
            "modified_time": self.modified_time,
            "mtime": self.modified_time,
            "source": "gamelist.xml" if self.gamelist_path else "filesystem",
            "metadata_source": "gamelist.xml" if self.gamelist_path else "filesystem",
            "entry_type": self.entry_type,
            "is_downloadable": self.is_downloadable,
            "image_stem": self.image_stem,
            "unique_id": self.unique_id,
            "gamelist_path": self.gamelist_path,
            "gamelist_game_id": self.gamelist_game_id,
            "has_gamelist_entry": bool(self.gamelist_path and self.gamelist_game_id),
        }
        if self.md5:
            payload["md5"] = self.md5
            payload["rom_md5"] = self.md5
        return payload


@dataclass(frozen=True)
class BiosCacheRow:
    entry_key: str
    file_path: str
    name: str
    unique_id: str
    absolute_path: str
    file_size: int
    modified_time: int
    md5: Optional[str]
    extra: dict

    @classmethod
    def from_payload(cls, entry_key: str, payload: dict) -> "BiosCacheRow":
        file_path = _normalize_path(payload.get("file_path") or payload.get("relative_path") or payload.get("path") or payload.get("name"))
        return cls(
            entry_key=entry_key,
            file_path=file_path,
            name=str(payload.get("name") or Path(file_path).name),
            unique_id=str(payload.get("unique_id") or ""),
            absolute_path=str(payload.get("absolute_path") or ""),
            file_size=_int(payload.get("file_size") or payload.get("byte_count") or payload.get("size")),
            modified_time=_int(payload.get("modified_time") or payload.get("mtime")),
            md5=_clean_optional(payload.get("bios_md5") or payload.get("md5")),
            extra={},
        )

    def to_payload(self) -> dict:
        payload = {
            **self.extra,
            "entry_type": "file",
            "name": self.name,
            "path": self.file_path,
            "file_path": self.file_path,
            "relative_path": self.file_path,
            "unique_id": self.unique_id,
            "file_size": self.file_size,
            "byte_count": self.file_size,
            "size": self.file_size,
            "modified_time": self.modified_time,
            "mtime": self.modified_time,
            "absolute_path": self.absolute_path,
        }
        if self.md5:
            payload["md5"] = self.md5
            payload["bios_md5"] = self.md5
        return payload


@dataclass(frozen=True)
class ArtworkCacheRow:
    entry_key: str
    system: str
    rom_path: str
    artwork_types: Tuple[str, ...]
    title: str
    file_path: str
    relative_path: str
    file_size: int
    modified_time: int
    md5: Optional[str]
    extra: dict

    @classmethod
    def from_payload(cls, entry_key: str, payload: dict) -> "ArtworkCacheRow":
        artwork_types = payload.get("artwork_types")
        if not isinstance(artwork_types, list):
            single_type = str(payload.get("artwork_type") or payload.get("type") or "").strip()
            artwork_types = [single_type] if single_type else []
        return cls(
            entry_key=entry_key,
            system=str(payload.get("system") or payload.get("system_name") or ""),
            rom_path=_normalize_path(payload.get("rom_path") or payload.get("file_path")),
            artwork_types=tuple(str(value) for value in artwork_types if str(value or "").strip()),
            title=str(payload.get("title") or payload.get("name") or ""),
            file_path=_normalize_path(payload.get("file_path")),
            relative_path=_normalize_path(payload.get("relative_path")),
            file_size=_int(payload.get("file_size") or payload.get("byte_count") or payload.get("size")),
            modified_time=_int(payload.get("modified_time") or payload.get("mtime")),
            md5=_clean_optional(payload.get("md5")),
            extra={},
        )

    def to_payload(self) -> dict:
        payload = {
            **self.extra,
            "asset_type": "artwork",
            "system": self.system,
            "system_name": self.system,
            "rom_path": self.rom_path,
            "file_path": self.file_path,
            "relative_path": self.relative_path,
            "title": self.title,
            "artwork_types": list(self.artwork_types),
            "metadata_source": "gamelist.xml",
            "file_size": self.file_size,
            "byte_count": self.file_size,
            "modified_time": self.modified_time,
            "mtime": self.modified_time,
        }
        if self.artwork_types:
            payload["artwork_type"] = self.artwork_types[0]
        if self.md5:
            payload["md5"] = self.md5
        return payload


def _normalize_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").lstrip("./")


def _clean_optional(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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
        "full_refresh_pending": False,
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
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS cache_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS asset_systems (name TEXT PRIMARY KEY, rom_count INTEGER NOT NULL DEFAULT 0)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS asset_gamelists ("
        "system TEXT PRIMARY KEY, path TEXT NOT NULL, exists_flag INTEGER NOT NULL DEFAULT 0, "
        "file_size INTEGER, modified_time INTEGER, rom_count INTEGER NOT NULL DEFAULT 0)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS rom_cache_entries ("
        "entry_key TEXT PRIMARY KEY, system TEXT NOT NULL, file_path TEXT NOT NULL, rom_name TEXT NOT NULL, "
        "unique_id TEXT, absolute_path TEXT, file_size INTEGER NOT NULL DEFAULT 0, modified_time INTEGER NOT NULL DEFAULT 0, "
        "entry_type TEXT NOT NULL DEFAULT 'file', md5 TEXT, gamelist_path TEXT, gamelist_game_id TEXT, "
        "is_downloadable INTEGER NOT NULL DEFAULT 1, image_stem TEXT, extra_json TEXT NOT NULL DEFAULT '{}', "
        "UNIQUE(system, file_path))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS bios_cache_entries ("
        "entry_key TEXT PRIMARY KEY, file_path TEXT NOT NULL UNIQUE, name TEXT NOT NULL, unique_id TEXT, absolute_path TEXT, "
        "file_size INTEGER NOT NULL DEFAULT 0, modified_time INTEGER NOT NULL DEFAULT 0, md5 TEXT, "
        "extra_json TEXT NOT NULL DEFAULT '{}')"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS artwork_cache_entries ("
        "entry_key TEXT PRIMARY KEY, system TEXT NOT NULL, rom_path TEXT NOT NULL, artwork_type TEXT, artwork_types TEXT NOT NULL DEFAULT '[]', title TEXT, "
        "file_path TEXT, relative_path TEXT, file_size INTEGER, modified_time INTEGER, md5 TEXT, extra_json TEXT NOT NULL DEFAULT '{}')"
    )
    _ensure_column(connection, "artwork_cache_entries", "artwork_types", "TEXT NOT NULL DEFAULT '[]'")
    for create_sql in {
        "deleted_rom_cache_entries": (
            "CREATE TABLE IF NOT EXISTS deleted_rom_cache_entries ("
            "entry_key TEXT PRIMARY KEY, system TEXT NOT NULL, file_path TEXT NOT NULL, rom_name TEXT NOT NULL, "
            "unique_id TEXT, absolute_path TEXT, file_size INTEGER NOT NULL DEFAULT 0, modified_time INTEGER NOT NULL DEFAULT 0, "
            "entry_type TEXT NOT NULL DEFAULT 'file', md5 TEXT, gamelist_path TEXT, gamelist_game_id TEXT, "
            "is_downloadable INTEGER NOT NULL DEFAULT 1, image_stem TEXT, extra_json TEXT NOT NULL DEFAULT '{}')"
        ),
        "deleted_bios_cache_entries": (
            "CREATE TABLE IF NOT EXISTS deleted_bios_cache_entries ("
            "entry_key TEXT PRIMARY KEY, file_path TEXT NOT NULL UNIQUE, name TEXT NOT NULL, unique_id TEXT, absolute_path TEXT, "
            "file_size INTEGER NOT NULL DEFAULT 0, modified_time INTEGER NOT NULL DEFAULT 0, md5 TEXT, "
            "extra_json TEXT NOT NULL DEFAULT '{}')"
        ),
        "deleted_artwork_cache_entries": (
            "CREATE TABLE IF NOT EXISTS deleted_artwork_cache_entries ("
            "entry_key TEXT PRIMARY KEY, system TEXT NOT NULL, rom_path TEXT NOT NULL, artwork_type TEXT, artwork_types TEXT NOT NULL DEFAULT '[]', title TEXT, "
            "file_path TEXT, relative_path TEXT, file_size INTEGER, modified_time INTEGER, md5 TEXT, extra_json TEXT NOT NULL DEFAULT '{}')"
        ),
    }.values():
        connection.execute(create_sql)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS cache_changes ("
        "asset_type TEXT NOT NULL, entry_key TEXT NOT NULL, operation TEXT NOT NULL, "
        "PRIMARY KEY (asset_type, entry_key))"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_rom_cache_system ON rom_cache_entries(system)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_artwork_cache_system ON artwork_cache_entries(system)")
    _migrate_blob_tables_if_needed(connection)
    _migrate_payload_change_queue_if_needed(connection)
    connection.execute(
        "INSERT INTO cache_state (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(ROM_METADATA_CACHE_VERSION),),
    )


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_blob_tables_if_needed(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'cache_entries'"
    ).fetchone()
    if not row:
        return
    for asset_type, entry_key, payload in connection.execute(
        "SELECT asset_type, entry_key, payload FROM cache_entries"
    ).fetchall():
        try:
            data = json.loads(payload)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if asset_type == "rom":
            _upsert_rom_row(connection, RomCacheRow.from_payload(str(entry_key), data))
        elif asset_type == "bios":
            _upsert_bios_row(connection, BiosCacheRow.from_payload(str(entry_key), data))
        elif asset_type == "artwork":
            _upsert_artwork_row(connection, str(entry_key), data)
    connection.execute("DROP TABLE cache_entries")


def _migrate_payload_change_queue_if_needed(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(cache_changes)")}
    if "payload" not in columns:
        return

    legacy_rows = connection.execute(
        "SELECT asset_type, entry_key, operation, payload FROM cache_changes"
    ).fetchall()
    connection.execute("ALTER TABLE cache_changes RENAME TO cache_changes_payload_legacy")
    connection.execute(
        "CREATE TABLE cache_changes ("
        "asset_type TEXT NOT NULL, entry_key TEXT NOT NULL, operation TEXT NOT NULL, "
        "PRIMARY KEY (asset_type, entry_key))"
    )
    for asset_type, entry_key, operation, payload in legacy_rows:
        asset_type = str(asset_type or "")
        entry_key = str(entry_key or "")
        operation = str(operation or "")
        if not asset_type or not entry_key or operation not in {"upsert", "delete"}:
            continue
        if operation == "delete":
            try:
                data = json.loads(payload)
            except Exception:
                data = {}
            if isinstance(data, dict):
                _upsert_deleted_row(connection, asset_type, entry_key, data)
        connection.execute(
            "INSERT INTO cache_changes (asset_type, entry_key, operation) VALUES (?, ?, ?) "
            "ON CONFLICT(asset_type, entry_key) DO UPDATE SET operation=excluded.operation",
            (asset_type, entry_key, operation),
        )
    connection.execute("DROP TABLE cache_changes_payload_legacy")


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
    rom_deleted_rows: Optional[dict] = None,
    bios_deleted_rows: Optional[dict] = None,
    artwork_deleted_rows: Optional[dict] = None,
    queue_changes: bool = True,
) -> None:
    """Persist changed metadata rows plus compact scan state."""
    state = {
        key: value
        for key, value in cache.items()
        if key not in {"entries", "bios_entries", "artwork_entries", "systems", "gamelists"}
    }
    state["schema_version"] = ROM_METADATA_CACHE_VERSION
    with _open_rom_metadata_cache(settings) as connection:
        connection.executemany(
            "INSERT INTO cache_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(key, json.dumps(value, sort_keys=True, default=str)) for key, value in state.items()],
        )
        if isinstance(cache.get("systems"), list):
            connection.execute("DELETE FROM asset_systems")
            connection.executemany(
                "INSERT INTO asset_systems (name, rom_count) VALUES (?, ?)",
                [
                    (str(row.get("name") or row.get("system_name") or ""), _int(row.get("rom_count")))
                    for row in cache["systems"]
                    if isinstance(row, dict) and str(row.get("name") or row.get("system_name") or "").strip()
                ],
            )
        if isinstance(cache.get("gamelists"), list):
            connection.execute("DELETE FROM asset_gamelists")
            connection.executemany(
                "INSERT INTO asset_gamelists (system, path, exists_flag, file_size, modified_time, rom_count) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(row.get("system") or row.get("system_name") or ""),
                        str(row.get("path") or row.get("file_path") or ""),
                        1 if row.get("exists") else 0,
                        _int(row.get("file_size")),
                        _int(row.get("modified_time")),
                        _int(row.get("rom_count")),
                    )
                    for row in cache["gamelists"]
                    if isinstance(row, dict) and str(row.get("system") or row.get("system_name") or "").strip()
                ],
            )
        _persist_rows(
            connection,
            "rom",
            rom_updates or {},
            rom_deletes or [],
            rom_deleted_rows or {},
            queue_changes=queue_changes,
        )
        _persist_rows(
            connection,
            "bios",
            bios_updates or {},
            bios_deletes or [],
            bios_deleted_rows or {},
            queue_changes=queue_changes,
        )
        _persist_rows(
            connection,
            "artwork",
            artwork_updates or {},
            artwork_deletes or [],
            artwork_deleted_rows or {},
            queue_changes=queue_changes,
        )


def _persist_rows(
    connection: sqlite3.Connection,
    asset_type: str,
    updates: dict,
    deletes: Iterable[str],
    deleted_rows: dict,
    *,
    queue_changes: bool,
) -> None:
    for key, value in updates.items():
        if not isinstance(value, dict):
            continue
        if asset_type == "rom":
            row = RomCacheRow.from_payload(str(key), value)
            _upsert_rom_row(connection, row)
        elif asset_type == "bios":
            row = BiosCacheRow.from_payload(str(key), value)
            _upsert_bios_row(connection, row)
        elif asset_type == "artwork":
            _upsert_artwork_row(connection, str(key), value)
        _delete_deleted_row(connection, asset_type, str(key))
        if queue_changes:
            _queue_change(connection, asset_type, str(key), "upsert")
    for key in deletes:
        key_text = str(key)
        table = {
            "rom": "rom_cache_entries",
            "bios": "bios_cache_entries",
            "artwork": "artwork_cache_entries",
        }[asset_type]
        deleted_payload = deleted_rows.get(key_text)
        if not isinstance(deleted_payload, dict):
            deleted_payload = _read_live_row_payload(connection, asset_type, key_text) or {"entry_key": key_text}
        _upsert_deleted_row(connection, asset_type, key_text, deleted_payload)
        connection.execute(f"DELETE FROM {table} WHERE entry_key = ?", (key_text,))
        if queue_changes:
            _queue_change(connection, asset_type, key_text, "delete")


def _upsert_rom_row(connection: sqlite3.Connection, row: RomCacheRow) -> None:
    connection.execute(
        "INSERT INTO rom_cache_entries (entry_key, system, file_path, rom_name, unique_id, absolute_path, file_size, "
        "modified_time, entry_type, md5, gamelist_path, gamelist_game_id, is_downloadable, image_stem, extra_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(entry_key) DO UPDATE SET system=excluded.system, file_path=excluded.file_path, rom_name=excluded.rom_name, "
        "unique_id=excluded.unique_id, absolute_path=excluded.absolute_path, file_size=excluded.file_size, "
        "modified_time=excluded.modified_time, entry_type=excluded.entry_type, md5=excluded.md5, "
        "gamelist_path=excluded.gamelist_path, gamelist_game_id=excluded.gamelist_game_id, "
        "is_downloadable=excluded.is_downloadable, image_stem=excluded.image_stem, extra_json=excluded.extra_json",
        (
            row.entry_key,
            row.system,
            row.file_path,
            row.rom_name,
            row.unique_id,
            row.absolute_path,
            row.file_size,
            row.modified_time,
            row.entry_type,
            row.md5,
            row.gamelist_path,
            row.gamelist_game_id,
            1 if row.is_downloadable else 0,
            row.image_stem,
            json.dumps(row.extra, sort_keys=True, default=str),
        ),
    )


def _upsert_bios_row(connection: sqlite3.Connection, row: BiosCacheRow) -> None:
    connection.execute(
        "INSERT INTO bios_cache_entries (entry_key, file_path, name, unique_id, absolute_path, file_size, modified_time, md5, extra_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(entry_key) DO UPDATE SET file_path=excluded.file_path, name=excluded.name, unique_id=excluded.unique_id, "
        "absolute_path=excluded.absolute_path, file_size=excluded.file_size, modified_time=excluded.modified_time, "
        "md5=excluded.md5, extra_json=excluded.extra_json",
        (
            row.entry_key,
            row.file_path,
            row.name,
            row.unique_id,
            row.absolute_path,
            row.file_size,
            row.modified_time,
            row.md5,
            json.dumps(row.extra, sort_keys=True, default=str),
        ),
    )


def _upsert_artwork_row(connection: sqlite3.Connection, entry_key: str, payload: dict) -> None:
    row = ArtworkCacheRow.from_payload(entry_key, payload)
    connection.execute(
        "INSERT INTO artwork_cache_entries (entry_key, system, rom_path, artwork_type, artwork_types, title, file_path, relative_path, file_size, modified_time, md5, extra_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(entry_key) DO UPDATE SET system=excluded.system, rom_path=excluded.rom_path, artwork_type=excluded.artwork_type, "
        "artwork_types=excluded.artwork_types, title=excluded.title, file_path=excluded.file_path, relative_path=excluded.relative_path, "
        "file_size=excluded.file_size, modified_time=excluded.modified_time, md5=excluded.md5, extra_json=excluded.extra_json",
        (
            entry_key,
            row.system,
            row.rom_path,
            row.artwork_types[0] if row.artwork_types else "",
            json.dumps(list(row.artwork_types), sort_keys=True),
            row.title,
            row.file_path,
            row.relative_path,
            row.file_size,
            row.modified_time,
            row.md5,
            json.dumps(row.extra, sort_keys=True, default=str),
        ),
    )


def _upsert_deleted_row(connection: sqlite3.Connection, asset_type: str, entry_key: str, payload: dict) -> None:
    if asset_type == "rom":
        row = RomCacheRow.from_payload(entry_key, payload)
        connection.execute(
            "INSERT INTO deleted_rom_cache_entries (entry_key, system, file_path, rom_name, unique_id, absolute_path, file_size, "
            "modified_time, entry_type, md5, gamelist_path, gamelist_game_id, is_downloadable, image_stem, extra_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entry_key) DO UPDATE SET system=excluded.system, file_path=excluded.file_path, rom_name=excluded.rom_name, "
            "unique_id=excluded.unique_id, absolute_path=excluded.absolute_path, file_size=excluded.file_size, "
            "modified_time=excluded.modified_time, entry_type=excluded.entry_type, md5=excluded.md5, "
            "gamelist_path=excluded.gamelist_path, gamelist_game_id=excluded.gamelist_game_id, "
            "is_downloadable=excluded.is_downloadable, image_stem=excluded.image_stem, extra_json=excluded.extra_json",
            (
                row.entry_key,
                row.system,
                row.file_path,
                row.rom_name,
                row.unique_id,
                row.absolute_path,
                row.file_size,
                row.modified_time,
                row.entry_type,
                row.md5,
                row.gamelist_path,
                row.gamelist_game_id,
                1 if row.is_downloadable else 0,
                row.image_stem,
                json.dumps(row.extra, sort_keys=True, default=str),
            ),
        )
    elif asset_type == "bios":
        row = BiosCacheRow.from_payload(entry_key, payload)
        connection.execute(
            "INSERT INTO deleted_bios_cache_entries (entry_key, file_path, name, unique_id, absolute_path, file_size, modified_time, md5, extra_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entry_key) DO UPDATE SET file_path=excluded.file_path, name=excluded.name, unique_id=excluded.unique_id, "
            "absolute_path=excluded.absolute_path, file_size=excluded.file_size, modified_time=excluded.modified_time, "
            "md5=excluded.md5, extra_json=excluded.extra_json",
            (
                row.entry_key,
                row.file_path,
                row.name,
                row.unique_id,
                row.absolute_path,
                row.file_size,
                row.modified_time,
                row.md5,
                json.dumps(row.extra, sort_keys=True, default=str),
            ),
        )
    elif asset_type == "artwork":
        row = ArtworkCacheRow.from_payload(entry_key, payload)
        connection.execute(
            "INSERT INTO deleted_artwork_cache_entries (entry_key, system, rom_path, artwork_type, artwork_types, title, file_path, relative_path, file_size, modified_time, md5, extra_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entry_key) DO UPDATE SET system=excluded.system, rom_path=excluded.rom_path, artwork_type=excluded.artwork_type, "
            "artwork_types=excluded.artwork_types, title=excluded.title, file_path=excluded.file_path, relative_path=excluded.relative_path, "
            "file_size=excluded.file_size, modified_time=excluded.modified_time, md5=excluded.md5, extra_json=excluded.extra_json",
            (
                row.entry_key,
                row.system,
                row.rom_path,
                row.artwork_types[0] if row.artwork_types else "",
                json.dumps(list(row.artwork_types), sort_keys=True),
                row.title,
                row.file_path,
                row.relative_path,
                row.file_size,
                row.modified_time,
                row.md5,
                json.dumps(row.extra, sort_keys=True, default=str),
            ),
        )


def _delete_deleted_row(connection: sqlite3.Connection, asset_type: str, entry_key: str) -> None:
    table = {
        "rom": "deleted_rom_cache_entries",
        "bios": "deleted_bios_cache_entries",
        "artwork": "deleted_artwork_cache_entries",
    }.get(asset_type)
    if table:
        connection.execute(f"DELETE FROM {table} WHERE entry_key = ?", (entry_key,))


def _queue_change(connection: sqlite3.Connection, asset_type: str, key: str, operation: str) -> None:
    connection.execute(
        "INSERT INTO cache_changes (asset_type, entry_key, operation) VALUES (?, ?, ?) "
        "ON CONFLICT(asset_type, entry_key) DO UPDATE SET operation=excluded.operation",
        (asset_type, key, operation),
    )


def _read_pending_rom_metadata_changes(settings: Any) -> dict:
    changes = {
        "roms": [],
        "bios": [],
        "artwork": [],
        "deleted": {"roms": [], "bios": [], "artwork": []},
    }
    labels = {"rom": "roms", "bios": "bios", "artwork": "artwork"}
    with _open_rom_metadata_cache(settings) as connection:
        for asset_type, entry_key, operation in connection.execute(
            "SELECT asset_type, entry_key, operation FROM cache_changes ORDER BY asset_type, entry_key"
        ):
            label = labels.get(asset_type)
            if not label:
                continue
            if operation == "delete":
                row = _read_deleted_row_payload(connection, asset_type, str(entry_key)) or {"entry_key": str(entry_key)}
            else:
                row = _read_live_row_payload(connection, asset_type, str(entry_key))
                if not row:
                    continue
            if operation == "delete":
                changes["deleted"][label].append(row)
            else:
                changes[label].append(row)
    return changes


def _clear_pending_rom_metadata_changes(settings: Any) -> None:
    with _open_rom_metadata_cache(settings) as connection:
        connection.execute("DELETE FROM cache_changes")
        connection.execute("DELETE FROM deleted_rom_cache_entries")
        connection.execute("DELETE FROM deleted_bios_cache_entries")
        connection.execute("DELETE FROM deleted_artwork_cache_entries")


def _clear_sqlite_asset_metadata_cache(settings: Any) -> None:
    """Clear Drone asset metadata while preserving unrelated app state in the shared DB."""
    tables = (
        "rom_cache_entries",
        "bios_cache_entries",
        "artwork_cache_entries",
        "deleted_rom_cache_entries",
        "deleted_bios_cache_entries",
        "deleted_artwork_cache_entries",
        "cache_changes",
        "asset_systems",
        "asset_gamelists",
    )
    with _open_rom_metadata_cache(settings) as connection:
        for table in tables:
            connection.execute(f"DELETE FROM {table}")
        connection.execute("DELETE FROM cache_state WHERE key <> 'schema_version'")
        connection.execute(
            "INSERT INTO cache_state (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(ROM_METADATA_CACHE_VERSION),),
        )
    try:
        _legacy_rom_metadata_cache_path(settings).unlink(missing_ok=True)
    except OSError:
        pass


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
        cache = {**_empty_rom_metadata_cache(), **state, "schema_version": ROM_METADATA_CACHE_VERSION}
        cache["systems"] = [
            {"name": name, "rom_count": rom_count}
            for name, rom_count in connection.execute("SELECT name, rom_count FROM asset_systems ORDER BY name")
        ]
        cache["gamelists"] = [
            {
                "system": system,
                "system_name": system,
                "path": path,
                "file_path": path,
                "exists": bool(exists_flag),
                "file_size": file_size,
                "modified_time": modified_time,
                "rom_count": rom_count,
            }
            for system, path, exists_flag, file_size, modified_time, rom_count in connection.execute(
                "SELECT system, path, exists_flag, file_size, modified_time, rom_count FROM asset_gamelists ORDER BY system"
            )
        ]
        cache["entries"] = _read_rom_rows(connection)
        cache["bios_entries"] = _read_bios_rows(connection)
        cache["artwork_entries"] = _read_artwork_rows(connection)
        return cache


def _read_sqlite_asset_systems(userdata_root: Path) -> Optional[list[dict]]:
    """Read cached system counts without materializing every cached ROM row."""
    try:
        db_path = _state_database_path(Path(userdata_root))
        with _open_state_database(db_path) as connection:
            _ensure_schema(connection)
            state = {
                key: json.loads(value)
                for key, value in connection.execute(
                    "SELECT key, value FROM cache_state WHERE key IN ('last_full_scan_at', 'scan_in_progress')"
                )
            }
            if not state.get("last_full_scan_at") or state.get("scan_in_progress"):
                return None
            rows = [
                {"name": name, "rom_count": _int(rom_count)}
                for name, rom_count in connection.execute("SELECT name, rom_count FROM asset_systems ORDER BY name")
                if str(name or "").strip() and _int(rom_count) > 0
            ]
            return rows or None
    except Exception:
        return None


def _read_rom_rows(connection: sqlite3.Connection) -> dict:
    rows = {}
    for values in connection.execute(
        "SELECT entry_key, system, file_path, rom_name, unique_id, absolute_path, file_size, modified_time, entry_type, "
        "md5, gamelist_path, gamelist_game_id, is_downloadable, image_stem, extra_json FROM rom_cache_entries"
    ):
        extra = _loads_dict(values[14])
        row = RomCacheRow(
            entry_key=values[0],
            system=values[1],
            file_path=values[2],
            rom_name=values[3],
            unique_id=values[4] or "",
            absolute_path=values[5] or "",
            file_size=_int(values[6]),
            modified_time=_int(values[7]),
            entry_type=values[8] or "file",
            md5=values[9],
            gamelist_path=values[10] or "",
            gamelist_game_id=values[11] or values[2],
            is_downloadable=bool(values[12]),
            image_stem=values[13] or Path(values[2]).stem,
            extra=extra,
        )
        rows[row.entry_key] = row.to_payload()
    return rows


def _read_live_row_payload(connection: sqlite3.Connection, asset_type: str, entry_key: str) -> Optional[dict]:
    table = {
        "rom": "rom_cache_entries",
        "bios": "bios_cache_entries",
        "artwork": "artwork_cache_entries",
    }.get(asset_type)
    return _read_row_payload_from_table(connection, asset_type, table, entry_key) if table else None


def _read_deleted_row_payload(connection: sqlite3.Connection, asset_type: str, entry_key: str) -> Optional[dict]:
    table = {
        "rom": "deleted_rom_cache_entries",
        "bios": "deleted_bios_cache_entries",
        "artwork": "deleted_artwork_cache_entries",
    }.get(asset_type)
    return _read_row_payload_from_table(connection, asset_type, table, entry_key) if table else None


def _read_row_payload_from_table(connection: sqlite3.Connection, asset_type: str, table: Optional[str], entry_key: str) -> Optional[dict]:
    if not table:
        return None
    if asset_type == "rom":
        values = connection.execute(
            f"SELECT entry_key, system, file_path, rom_name, unique_id, absolute_path, file_size, modified_time, entry_type, "
            f"md5, gamelist_path, gamelist_game_id, is_downloadable, image_stem, extra_json FROM {table} WHERE entry_key = ?",
            (entry_key,),
        ).fetchone()
        if not values:
            return None
        return RomCacheRow(
            entry_key=values[0],
            system=values[1],
            file_path=values[2],
            rom_name=values[3],
            unique_id=values[4] or "",
            absolute_path=values[5] or "",
            file_size=_int(values[6]),
            modified_time=_int(values[7]),
            entry_type=values[8] or "file",
            md5=values[9],
            gamelist_path=values[10] or "",
            gamelist_game_id=values[11] or values[2],
            is_downloadable=bool(values[12]),
            image_stem=values[13] or Path(values[2]).stem,
            extra=_loads_dict(values[14]),
        ).to_payload()
    if asset_type == "bios":
        values = connection.execute(
            f"SELECT entry_key, file_path, name, unique_id, absolute_path, file_size, modified_time, md5, extra_json FROM {table} WHERE entry_key = ?",
            (entry_key,),
        ).fetchone()
        if not values:
            return None
        return BiosCacheRow(
            entry_key=values[0],
            file_path=values[1],
            name=values[2],
            unique_id=values[3] or "",
            absolute_path=values[4] or "",
            file_size=_int(values[5]),
            modified_time=_int(values[6]),
            md5=values[7],
            extra=_loads_dict(values[8]),
        ).to_payload()
    if asset_type == "artwork":
        values = connection.execute(
            f"SELECT entry_key, system, rom_path, artwork_type, artwork_types, title, file_path, relative_path, file_size, modified_time, md5, extra_json "
            f"FROM {table} WHERE entry_key = ?",
            (entry_key,),
        ).fetchone()
        if not values:
            return None
        artwork_types = _loads_list(values[4]) or ([values[3]] if values[3] else [])
        return ArtworkCacheRow(
            entry_key=values[0],
            system=values[1],
            rom_path=values[2],
            artwork_types=tuple(str(value) for value in artwork_types if str(value or "").strip()),
            title=values[5] or "",
            file_path=values[6] or "",
            relative_path=values[7] or "",
            file_size=_int(values[8]),
            modified_time=_int(values[9]),
            md5=values[10],
            extra=_loads_dict(values[11]),
        ).to_payload()
    return None


def _read_bios_rows(connection: sqlite3.Connection) -> dict:
    rows = {}
    for values in connection.execute(
        "SELECT entry_key, file_path, name, unique_id, absolute_path, file_size, modified_time, md5, extra_json FROM bios_cache_entries"
    ):
        row = BiosCacheRow(
            entry_key=values[0],
            file_path=values[1],
            name=values[2],
            unique_id=values[3] or "",
            absolute_path=values[4] or "",
            file_size=_int(values[5]),
            modified_time=_int(values[6]),
            md5=values[7],
            extra=_loads_dict(values[8]),
        )
        rows[row.entry_key] = row.to_payload()
    return rows


def _read_artwork_rows(connection: sqlite3.Connection) -> dict:
    rows = {}
    for values in connection.execute(
        "SELECT entry_key, system, rom_path, artwork_type, artwork_types, title, file_path, relative_path, file_size, modified_time, md5, extra_json "
        "FROM artwork_cache_entries"
    ):
        artwork_types = _loads_list(values[4]) or ([values[3]] if values[3] else [])
        row = ArtworkCacheRow(
            entry_key=values[0],
            system=values[1],
            rom_path=values[2],
            artwork_types=tuple(str(value) for value in artwork_types if str(value or "").strip()),
            title=values[5] or "",
            file_path=values[6] or "",
            relative_path=values[7] or "",
            file_size=_int(values[8]),
            modified_time=_int(values[9]),
            md5=values[10],
            extra=_loads_dict(values[11]),
        )
        rows[row.entry_key] = row.to_payload()
    return rows


def _loads_list(value: Any) -> list:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _loads_dict(value: Any) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _load_rom_metadata_cache(settings: Any) -> Tuple[dict, bool]:
    path = _rom_metadata_cache_path(settings)
    legacy_path = _legacy_rom_metadata_cache_path(settings)
    try:
        if path.exists():
            return _read_sqlite_rom_metadata_cache(settings), False
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
            print(f"Asset metadata cache migrated to relational store: {path}", file=sys.stdout, flush=True)
            return _read_sqlite_rom_metadata_cache(settings), False
        return _empty_rom_metadata_cache(), True
    except Exception as error:
        print(f"Asset metadata cache rebuild required: {path} ({_format_store_error(error)})", file=sys.stderr, flush=True)
        if path.exists():
            try:
                path.replace(path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex}"))
            except Exception:
                pass
        return _empty_rom_metadata_cache(), True
