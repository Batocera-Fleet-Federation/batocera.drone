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
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import open_database as _open_state_database  # type: ignore


ROM_METADATA_CACHE_VERSION = 5
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
    "fingerprint",
    "rom_fingerprint",
    "md5",
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
    fingerprint: Optional[str]
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
            fingerprint=_clean_optional(payload.get("rom_fingerprint") or payload.get("fingerprint")),
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
        if self.fingerprint:
            payload["fingerprint"] = self.fingerprint
            payload["rom_fingerprint"] = self.fingerprint
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
    fingerprint: Optional[str]
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
            fingerprint=_clean_optional(payload.get("fingerprint")),
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
        if self.fingerprint:
            payload["fingerprint"] = self.fingerprint
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


def _is_transient_sqlite_open_error(error: BaseException) -> bool:
    message = str(error).lower()
    return isinstance(error, sqlite3.OperationalError) and (
        "unable to open database file" in message
        or "database is locked" in message
        or "readonly database" in message
    )


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
        "entry_type TEXT NOT NULL DEFAULT 'file', fingerprint TEXT, gamelist_path TEXT, gamelist_game_id TEXT, "
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
        "file_path TEXT, relative_path TEXT, file_size INTEGER, modified_time INTEGER, fingerprint TEXT, extra_json TEXT NOT NULL DEFAULT '{}')"
    )
    _ensure_column(connection, "artwork_cache_entries", "artwork_types", "TEXT NOT NULL DEFAULT '[]'")
    for create_sql in {
        "deleted_rom_cache_entries": (
            "CREATE TABLE IF NOT EXISTS deleted_rom_cache_entries ("
            "entry_key TEXT PRIMARY KEY, system TEXT NOT NULL, file_path TEXT NOT NULL, rom_name TEXT NOT NULL, "
            "unique_id TEXT, absolute_path TEXT, file_size INTEGER NOT NULL DEFAULT 0, modified_time INTEGER NOT NULL DEFAULT 0, "
            "entry_type TEXT NOT NULL DEFAULT 'file', fingerprint TEXT, gamelist_path TEXT, gamelist_game_id TEXT, "
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
            "file_path TEXT, relative_path TEXT, file_size INTEGER, modified_time INTEGER, fingerprint TEXT, extra_json TEXT NOT NULL DEFAULT '{}')"
        ),
    }.values():
        connection.execute(create_sql)
    _migrate_md5_column_to_fingerprint(connection)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS cache_changes ("
        "asset_type TEXT NOT NULL, entry_key TEXT NOT NULL, operation TEXT NOT NULL, "
        "PRIMARY KEY (asset_type, entry_key))"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_rom_cache_system ON rom_cache_entries(system)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_artwork_cache_system ON artwork_cache_entries(system)")
    # Btree indexes that back ORDER BY and the LIKE fallback when FTS5 is unavailable.
    connection.execute("CREATE INDEX IF NOT EXISTS idx_rom_cache_name ON rom_cache_entries(rom_name)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_rom_cache_system_name ON rom_cache_entries(system, rom_name)")
    _ensure_rom_search_index(connection)
    _migrate_blob_tables_if_needed(connection)
    _migrate_payload_change_queue_if_needed(connection)
    connection.execute(
        "INSERT INTO cache_state (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(ROM_METADATA_CACHE_VERSION),),
    )


ROM_FTS_MIN_QUERY_LENGTH = 3  # trigram tokenizer requires >= 3 chars to MATCH


def _fts5_trigram_supported(connection: sqlite3.Connection) -> bool:
    """Return True if this SQLite build has FTS5 with the trigram tokenizer."""
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp._fts5_probe USING fts5(x, tokenize='trigram')"
        )
        connection.execute("DROP TABLE IF EXISTS temp._fts5_probe")
        return True
    except sqlite3.Error:
        return False


def _ensure_rom_search_index(connection: sqlite3.Connection) -> None:
    """Create the FTS5 (trigram) index over rom_cache_entries plus its sync triggers.

    Uses an external-content FTS table so no column data is duplicated, and keeps it
    in sync with triggers (covers every upsert/delete path automatically). Degrades
    gracefully to the btree LIKE fallback when FTS5/trigram is unavailable on the host
    SQLite build (e.g. an older Batocera image).
    """
    already_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rom_fts'"
    ).fetchone()
    if not _fts5_trigram_supported(connection):
        return
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS rom_fts USING fts5("
            "rom_name, content='rom_cache_entries', content_rowid='rowid', tokenize='trigram')"
        )
        connection.execute(
            "CREATE TRIGGER IF NOT EXISTS rom_fts_ai AFTER INSERT ON rom_cache_entries BEGIN "
            "INSERT INTO rom_fts(rowid, rom_name) VALUES (new.rowid, new.rom_name); END"
        )
        connection.execute(
            "CREATE TRIGGER IF NOT EXISTS rom_fts_ad AFTER DELETE ON rom_cache_entries BEGIN "
            "INSERT INTO rom_fts(rom_fts, rowid, rom_name) VALUES ('delete', old.rowid, old.rom_name); END"
        )
        connection.execute(
            "CREATE TRIGGER IF NOT EXISTS rom_fts_au AFTER UPDATE ON rom_cache_entries BEGIN "
            "INSERT INTO rom_fts(rom_fts, rowid, rom_name) VALUES ('delete', old.rowid, old.rom_name); "
            "INSERT INTO rom_fts(rowid, rom_name) VALUES (new.rowid, new.rom_name); END"
        )
        if not already_exists:
            # Backfill the index from existing rows (first-time creation only).
            connection.execute("INSERT INTO rom_fts(rom_fts) VALUES ('rebuild')")
    except sqlite3.Error as error:
        # Never let an index problem break cache open; fall back to LIKE scans.
        print(f"ROM search index unavailable, using LIKE fallback: {_format_store_error(error)}", file=sys.stderr, flush=True)


def _rom_search_index_present(connection: sqlite3.Connection) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rom_fts'"
        ).fetchone()
    )


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_md5_column_to_fingerprint(connection: sqlite3.Connection) -> None:
    """Rename the legacy ``md5`` column to ``fingerprint`` on pre-v5 ROM/artwork caches.

    The old ROM column held full-file md5 / gamelist md5 values; the ROM fingerprint is
    now a sampled hash (``sample-fp-v1``), a different algorithm, so the rows are renamed
    and their values cleared to force a clean re-fingerprint on the next scan. BIOS tables
    are intentionally excluded — BIOS keeps a full-file ``md5`` column (exact emulator
    identity), so there is nothing to rename there.
    """
    tables = (
        "rom_cache_entries", "artwork_cache_entries",
        "deleted_rom_cache_entries", "deleted_artwork_cache_entries",
        "preserved_rom_fingerprint",
    )
    for table in tables:
        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if not exists:
            continue
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "md5" in columns and "fingerprint" not in columns:
            connection.execute(f"ALTER TABLE {table} RENAME COLUMN md5 TO fingerprint")
            # Discard stale (wrong-algorithm) values so the next scan recomputes them.
            connection.execute(f"UPDATE {table} SET fingerprint = NULL")


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
        "modified_time, entry_type, fingerprint, gamelist_path, gamelist_game_id, is_downloadable, image_stem, extra_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(entry_key) DO UPDATE SET system=excluded.system, file_path=excluded.file_path, rom_name=excluded.rom_name, "
        "unique_id=excluded.unique_id, absolute_path=excluded.absolute_path, file_size=excluded.file_size, "
        "modified_time=excluded.modified_time, entry_type=excluded.entry_type, fingerprint=excluded.fingerprint, "
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
            row.fingerprint,
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
        "INSERT INTO artwork_cache_entries (entry_key, system, rom_path, artwork_type, artwork_types, title, file_path, relative_path, file_size, modified_time, fingerprint, extra_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(entry_key) DO UPDATE SET system=excluded.system, rom_path=excluded.rom_path, artwork_type=excluded.artwork_type, "
        "artwork_types=excluded.artwork_types, title=excluded.title, file_path=excluded.file_path, relative_path=excluded.relative_path, "
        "file_size=excluded.file_size, modified_time=excluded.modified_time, fingerprint=excluded.fingerprint, extra_json=excluded.extra_json",
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
            row.fingerprint,
            json.dumps(row.extra, sort_keys=True, default=str),
        ),
    )


def _upsert_deleted_row(connection: sqlite3.Connection, asset_type: str, entry_key: str, payload: dict) -> None:
    if asset_type == "rom":
        row = RomCacheRow.from_payload(entry_key, payload)
        connection.execute(
            "INSERT INTO deleted_rom_cache_entries (entry_key, system, file_path, rom_name, unique_id, absolute_path, file_size, "
            "modified_time, entry_type, fingerprint, gamelist_path, gamelist_game_id, is_downloadable, image_stem, extra_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entry_key) DO UPDATE SET system=excluded.system, file_path=excluded.file_path, rom_name=excluded.rom_name, "
            "unique_id=excluded.unique_id, absolute_path=excluded.absolute_path, file_size=excluded.file_size, "
            "modified_time=excluded.modified_time, entry_type=excluded.entry_type, fingerprint=excluded.fingerprint, "
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
                row.fingerprint,
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
            "INSERT INTO deleted_artwork_cache_entries (entry_key, system, rom_path, artwork_type, artwork_types, title, file_path, relative_path, file_size, modified_time, fingerprint, extra_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entry_key) DO UPDATE SET system=excluded.system, rom_path=excluded.rom_path, artwork_type=excluded.artwork_type, "
            "artwork_types=excluded.artwork_types, title=excluded.title, file_path=excluded.file_path, relative_path=excluded.relative_path, "
            "file_size=excluded.file_size, modified_time=excluded.modified_time, fingerprint=excluded.fingerprint, extra_json=excluded.extra_json",
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
                row.fingerprint,
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


def _ensure_preserved_fingerprint_tables(connection: sqlite3.Connection) -> None:
    """Create the identity-preservation tables used to survive a cache purge.

    ROMs use a sampled ``fingerprint``; BIOS use a full-file ``md5`` (exact emulator
    identity), so the preservation columns mirror that split.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS preserved_rom_fingerprint ("
        "entry_key TEXT PRIMARY KEY, file_size INTEGER, modified_time INTEGER, fingerprint TEXT)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS preserved_bios_md5 ("
        "entry_key TEXT PRIMARY KEY, file_size INTEGER, modified_time INTEGER, md5 TEXT)"
    )
    # Pre-v5 ROM caches may still carry a legacy ``md5`` column on the ROM tables.
    _migrate_md5_column_to_fingerprint(connection)


def _read_preserved_asset_fingerprint(settings: Any) -> dict:
    """Return identity snapshots saved by the last purge, keyed by entry_key.

    Shape: ``{"rom": {entry_key: {file_size, modified_time, fingerprint}},
              "bios": {entry_key: {file_size, modified_time, md5}}}``.
    Empty when nothing was preserved; safe to call on caches that never purged.
    """
    result: dict = {"rom": {}, "bios": {}}
    with _open_rom_metadata_cache(settings) as connection:
        _ensure_preserved_fingerprint_tables(connection)
        for table, bucket, column in (
            ("preserved_rom_fingerprint", "rom", "fingerprint"),
            ("preserved_bios_md5", "bios", "md5"),
        ):
            for entry_key, file_size, modified_time, value in connection.execute(
                f"SELECT entry_key, file_size, modified_time, {column} FROM {table}"
            ):
                if not entry_key or not value:
                    continue
                result[bucket][entry_key] = {
                    "file_size": file_size,
                    "modified_time": modified_time,
                    column: value,
                }
    return result


def _purge_asset_cache_keep_fingerprint(settings: Any, requested_at: Optional[str] = None) -> dict:
    """Clear cached asset metadata and queue a full rebuild, preserving fingerprint.

    This empties the rom/bios/artwork entry tables (so the cached counts drop to
    zero and a clean rebuild runs) and clears the stored ROM-inventory
    fingerprint. Before clearing, it snapshots every known fingerprint keyed by
    ``(entry_key, file_size, modified_time)`` into ``preserved_*`` tables. The
    next metadata poll rebuilds the entry set from disk and reuses those fingerprint
    values for unchanged files (see ``_read_preserved_asset_fingerprint``), so ROMs are
    not re-hashed, then uploads a full ``replace_all`` inventory to Overmind.
    """
    from datetime import datetime, timezone

    requested = requested_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cleared = {"roms": 0, "bios": 0, "artwork": 0, "preserved_fingerprint": 0}
    with _open_rom_metadata_cache(settings) as connection:
        _ensure_preserved_fingerprint_tables(connection)
        # Snapshot fingerprint before clearing so the rebuild does not re-hash unchanged files.
        connection.execute("DELETE FROM preserved_rom_fingerprint")
        preserved = connection.execute(
            "INSERT INTO preserved_rom_fingerprint (entry_key, file_size, modified_time, fingerprint) "
            "SELECT entry_key, file_size, modified_time, fingerprint FROM rom_cache_entries "
            "WHERE fingerprint IS NOT NULL AND fingerprint <> ''"
        ).rowcount
        connection.execute("DELETE FROM preserved_bios_md5")
        connection.execute(
            "INSERT INTO preserved_bios_md5 (entry_key, file_size, modified_time, md5) "
            "SELECT entry_key, file_size, modified_time, md5 FROM bios_cache_entries "
            "WHERE md5 IS NOT NULL AND md5 <> ''"
        )
        cleared["roms"] = connection.execute("SELECT count(*) FROM rom_cache_entries").fetchone()[0]
        cleared["bios"] = connection.execute("SELECT count(*) FROM bios_cache_entries").fetchone()[0]
        cleared["artwork"] = connection.execute("SELECT count(*) FROM artwork_cache_entries").fetchone()[0]
        cleared["preserved_fingerprint"] = max(0, int(preserved or 0))
        # Empty the asset entry + derived tables (cached counts drop to zero).
        for table in (
            "rom_cache_entries",
            "bios_cache_entries",
            "artwork_cache_entries",
            "deleted_rom_cache_entries",
            "deleted_bios_cache_entries",
            "deleted_artwork_cache_entries",
            "cache_changes",
            "asset_systems",
            "asset_gamelists",
        ):
            connection.execute(f"DELETE FROM {table}")
        # Drop fingerprint + upload/scan markers so a full rebuild and upload run.
        for key in (
            "rom_inventory_fingerprint",
            "rom_inventory_fingerprint_algorithm",
            "last_full_scan_at",
            "last_successful_upload_at",
            "scan_checkpoint_at",
        ):
            connection.execute("DELETE FROM cache_state WHERE key = ?", (key,))
        for key, value in (
            ("dirty", True),
            ("full_refresh_pending", True),
            ("scan_in_progress", False),
            ("rebuild_requested_at", requested),
        ):
            connection.execute(
                "INSERT INTO cache_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, sort_keys=True, default=str)),
            )
    return {"status": "queued", "requested_at": requested, "cleared": cleared}


def _update_rom_metadata_cache_state(settings: Any, **values: Any) -> None:
    """Update compact scan/upload state without reading all cached asset rows."""
    if not values:
        return
    with _open_rom_metadata_cache(settings) as connection:
        connection.executemany(
            "INSERT INTO cache_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(key, json.dumps(value, sort_keys=True, default=str)) for key, value in values.items()],
        )


def _read_rom_metadata_cache_state(settings: Any, *keys: str) -> dict:
    """Read compact scan/upload state without materializing cached asset rows."""
    with _open_rom_metadata_cache(settings) as connection:
        if keys:
            placeholders = ",".join("?" for _ in keys)
            rows = connection.execute(
                f"SELECT key, value FROM cache_state WHERE key IN ({placeholders})",
                tuple(keys),
            )
        else:
            rows = connection.execute("SELECT key, value FROM cache_state")
        state = {}
        for key, value in rows:
            try:
                state[key] = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                state[key] = value
        return state


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
        "fingerprint, gamelist_path, gamelist_game_id, is_downloadable, image_stem, extra_json FROM rom_cache_entries"
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
            fingerprint=values[9],
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
            f"fingerprint, gamelist_path, gamelist_game_id, is_downloadable, image_stem, extra_json FROM {table} WHERE entry_key = ?",
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
            fingerprint=values[9],
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
            f"SELECT entry_key, system, rom_path, artwork_type, artwork_types, title, file_path, relative_path, file_size, modified_time, fingerprint, extra_json "
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
            fingerprint=values[10],
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
        "SELECT entry_key, system, rom_path, artwork_type, artwork_types, title, file_path, relative_path, file_size, modified_time, fingerprint, extra_json "
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
            fingerprint=values[10],
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


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so user input is treated as a literal substring."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_rom_entries(
    settings: Any,
    query: str,
    *,
    system_filter: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[dict]:
    """Search ROM names directly in SQLite (FTS5 trigram, or LIKE fallback).

    Returns lightweight result rows ``{system, name, unique_id, is_downloadable,
    image_stem, fingerprint}`` straight from the relational columns — no full-cache
    materialization, no per-row JSON parsing, filtering/ordering/pagination all run
    in SQLite. This is the fast path that replaces the in-memory linear scan.
    """
    normalized = (query or "").strip()
    if not normalized:
        return []
    params: list = []
    select = (
        "SELECT system, rom_name, unique_id, is_downloadable, image_stem, fingerprint "
        "FROM rom_cache_entries "
    )
    try:
        with _open_rom_metadata_cache(settings) as connection:
            use_fts = len(normalized) >= ROM_FTS_MIN_QUERY_LENGTH and _rom_search_index_present(connection)
            if use_fts:
                where = "WHERE rowid IN (SELECT rowid FROM rom_fts WHERE rom_fts MATCH ?) "
                params.append('"' + normalized.replace('"', '""') + '"')
            else:
                where = "WHERE rom_name LIKE ? ESCAPE '\\' "
                params.append(f"%{_escape_like(normalized)}%")
            if system_filter:
                where += "AND system = ? COLLATE NOCASE "
                params.append(system_filter)
            order = "ORDER BY system COLLATE NOCASE, rom_name COLLATE NOCASE "
            limit_clause = ""
            if limit is not None and int(limit) > 0:
                limit_clause = "LIMIT ? OFFSET ?"
                params.extend([int(limit), max(0, int(offset))])
            rows = connection.execute(select + where + order + limit_clause, params).fetchall()
    except sqlite3.Error as error:
        print(f"ROM search query failed: {_format_store_error(error)}", file=sys.stderr, flush=True)
        return []
    return [
        {
            "system": row[0],
            "name": row[1],
            "unique_id": row[2] or "",
            "is_downloadable": bool(row[3]),
            "image_stem": row[4],
            "fingerprint": row[5],
        }
        for row in rows
    ]


def rom_cache_has_entries(settings: Any) -> bool:
    """Return True if the relational ROM cache holds any rows (cheap existence check)."""
    try:
        with _open_rom_metadata_cache(settings) as connection:
            return connection.execute("SELECT 1 FROM rom_cache_entries LIMIT 1").fetchone() is not None
    except Exception:
        return False


def rom_cache_ready(settings: Any) -> bool:
    """Return True once a full scan has completed and none is in progress.

    Mirrors the readiness gate of the legacy in-memory snapshot so cache-backed
    listings only serve results when the relational cache is authoritative; during
    the initial/active scan callers fall back to scanning the live filesystem.
    Reads only two ``cache_state`` keys — no row materialization.
    """
    try:
        with _open_rom_metadata_cache(settings) as connection:
            state = dict(
                connection.execute(
                    "SELECT key, value FROM cache_state WHERE key IN ('last_full_scan_at', 'scan_in_progress')"
                ).fetchall()
            )
            last_full_scan = json.loads(state.get("last_full_scan_at") or "null")
            scan_in_progress = json.loads(state.get("scan_in_progress") or "false")
            return bool(last_full_scan) and not bool(scan_in_progress)
    except Exception:
        return False


def list_rom_rows_by_system(settings: Any, system: str, *, include_fingerprint: bool = True) -> Optional[List[dict]]:
    """Return ROM rows for a single system straight from SQLite, ordered by name.

    Uses the ``idx_rom_cache_system`` index so listing one system never loads the
    whole library into memory — the key win for large libraries on small hardware.
    Returns ``None`` only on a SQL error so the caller can fall back to the filesystem.
    """
    columns = "system, file_path, rom_name, unique_id, file_size, entry_type, is_downloadable, image_stem, fingerprint"
    try:
        with _open_rom_metadata_cache(settings) as connection:
            rows = connection.execute(
                f"SELECT {columns} FROM rom_cache_entries WHERE system = ? COLLATE NOCASE "
                "ORDER BY rom_name COLLATE NOCASE",
                (system,),
            ).fetchall()
    except sqlite3.Error as error:
        print(f"ROM system listing failed: {_format_store_error(error)}", file=sys.stderr, flush=True)
        return None
    result: List[dict] = []
    for row in rows:
        item = {
            "system": row[0],
            "file_path": row[1],
            "rom_name": row[2],
            "unique_id": row[3] or "",
            "file_size": _int(row[4]),
            "entry_type": row[5] or "file",
            "is_downloadable": bool(row[6]),
            "image_stem": row[7],
        }
        if include_fingerprint:
            item["fingerprint"] = row[8]
        result.append(item)
    return result


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
        if _is_transient_sqlite_open_error(error):
            print(
                f"Asset metadata cache temporarily unavailable; preserving existing database for next poll: {path}",
                file=sys.stderr,
                flush=True,
            )
            return _empty_rom_metadata_cache(), False
        print(f"Asset metadata cache rebuild required: {path} ({_format_store_error(error)})", file=sys.stderr, flush=True)
        if path.exists():
            try:
                path.replace(path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex}"))
            except Exception:
                pass
        return _empty_rom_metadata_cache(), True
