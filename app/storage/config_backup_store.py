"""Relational metadata for config-backup tarballs (the "Backups" admin tile).

Same shared-sqlite-file convention as every other ``storage/*_store.py``
module (see ``audit_store.py`` for the closest example): one physical
database (``state_store.database_path``), this module owns its own table via
its own idempotent ``_ensure_schema()``.

Deliberately metadata-only -- the actual tarball bytes live on disk under
``config_backup.backups_directory()``, not as a SQLite blob (see the DB
skill's "avoid storing large binary blobs in SQLite" rule; a backup can
legitimately be several hundred MB to a couple GB once saves are included).
Each row just tracks where its file lives, how big it is, and what happened
while building it -- exactly the pattern ROM/torrent files already follow
elsewhere in this app (bytes on disk, SQLite tracks the record).

Config backups are also a P2P asset type (see ``handlers_peer.py``'s
``config_backups`` branch and ``transfer/peer_download.py``'s
``_download_config_backup_from_peer``): a backup pulled from a paired peer
gets its own new local row via ``record_downloaded()``, with the
``source_drone_*`` columns populated so it's visibly distinguishable from a
locally-created one. There is no single-hop re-share restriction here (unlike
VPN/SMTP configs) -- backups don't carry secrets by design, so a downloaded
backup is just as freely re-shareable onward as a ROM would be.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from .state_store import database_path as _state_database_path
    from .state_store import open_database as _open_state_database
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import open_database as _open_state_database  # type: ignore


STATUS_CREATING = "creating"
STATUS_COMPLETE = "complete"
STATUS_ERROR = "error"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _db_path(userdata_root: Path) -> Path:
    return _state_database_path(userdata_root)


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _open(userdata_root: Path) -> sqlite3.Connection:
    connection = _open_state_database(_db_path(userdata_root))
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS config_backups ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "status TEXT NOT NULL DEFAULT 'creating', "
        "file_name TEXT NOT NULL, "
        "size_bytes INTEGER NOT NULL DEFAULT 0, "
        "included_file_count INTEGER NOT NULL DEFAULT 0, "
        "skipped_file_count INTEGER NOT NULL DEFAULT 0, "
        "skipped_bytes INTEGER NOT NULL DEFAULT 0, "
        "error_message TEXT, "
        "created_at TEXT NOT NULL, "
        "completed_at TEXT)"
    )
    # Added after the initial release -- _ensure_column so an already-deployed
    # Drone upgrades in place instead of needing its config_backups table
    # dropped (see the drone-db-management skill: never bump applied schema
    # in place, add columns idempotently).
    _ensure_column(connection, "config_backups", "name", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "config_backups", "description", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "config_backups", "source_drone_id", "TEXT")
    _ensure_column(connection, "config_backups", "source_drone_name", "TEXT")
    _ensure_column(connection, "config_backups", "source_created_at", "TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_config_backups_created_at ON config_backups(created_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_config_backups_status ON config_backups(status)")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_config_backups_file_name ON config_backups(file_name)")
    connection.commit()


def _row_to_dict(row: tuple) -> dict:
    return {
        "id": row[0],
        "status": row[1],
        "file_name": row[2],
        # Alias so the generic peer-asset-browsing frontend (localAssetPath/
        # localAssetDisplayName, shared with every other asset type) has a
        # sensible fallback identity even when no custom `name` was set --
        # the same key roms/bios/saves/movies already use for this purpose.
        "relative_path": row[2],
        "size_bytes": row[3],
        # Same reasoning: the generic peer-browse table's Size column reads
        # file_size/byte_count (whatever every other asset type happens to
        # use), and its Details column falls back to modified_at for a date.
        "file_size": row[3],
        "byte_count": row[3],
        "modified_at": row[8],
        "included_file_count": row[4],
        "skipped_file_count": row[5],
        "skipped_bytes": row[6],
        "error_message": row[7],
        "created_at": row[8],
        "completed_at": row[9],
        "name": row[10] or "",
        "description": row[11] or "",
        "source_drone_id": row[12],
        "source_drone_name": row[13],
        "source_created_at": row[14],
        "is_local": row[12] is None,
    }


_COLUMNS = (
    "id, status, file_name, size_bytes, included_file_count, skipped_file_count, "
    "skipped_bytes, error_message, created_at, completed_at, name, description, "
    "source_drone_id, source_drone_name, source_created_at"
)


def create_pending(settings: Any, file_name: str, *, name: str = "", description: str = "") -> dict:
    """Insert the "creating" row a background job will fill in once it finishes."""
    created_at = _now()
    with _open(settings.userdata_root) as connection:
        cursor = connection.execute(
            "INSERT INTO config_backups (status, file_name, created_at, name, description) "
            "VALUES (?, ?, ?, ?, ?)",
            (STATUS_CREATING, file_name, created_at, str(name or ""), str(description or "")),
        )
        backup_id = cursor.lastrowid
    return {
        "id": backup_id,
        "status": STATUS_CREATING,
        "file_name": file_name,
        "size_bytes": 0,
        "included_file_count": 0,
        "skipped_file_count": 0,
        "skipped_bytes": 0,
        "error_message": None,
        "created_at": created_at,
        "completed_at": None,
        "name": str(name or ""),
        "description": str(description or ""),
        "source_drone_id": None,
        "source_drone_name": None,
        "source_created_at": None,
        "is_local": True,
    }


def mark_complete(
    settings: Any,
    backup_id: int,
    *,
    size_bytes: int,
    included_file_count: int,
    skipped_file_count: int,
    skipped_bytes: int,
) -> None:
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "UPDATE config_backups SET status = ?, size_bytes = ?, included_file_count = ?, "
            "skipped_file_count = ?, skipped_bytes = ?, completed_at = ? WHERE id = ?",
            (STATUS_COMPLETE, size_bytes, included_file_count, skipped_file_count, skipped_bytes, _now(), backup_id),
        )


def mark_error(settings: Any, backup_id: int, message: str) -> None:
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "UPDATE config_backups SET status = ?, error_message = ?, completed_at = ? WHERE id = ?",
            (STATUS_ERROR, str(message or "backup failed"), _now(), backup_id),
        )


def record_downloaded(
    settings: Any,
    *,
    file_name: str,
    size_bytes: int,
    name: str,
    description: str,
    source_drone_id: str,
    source_drone_name: str,
    source_created_at: Optional[str],
) -> dict:
    """Register a backup tarball just pulled from a peer as a new local row,
    already "complete" (the file is already fully on disk by the time this is
    called -- see ``_download_config_backup_from_peer``). Distinct from
    ``create_pending`` + ``mark_complete`` (the locally-built path) because
    there's no in-progress phase to track for something that arrived whole."""
    created_at = _now()
    with _open(settings.userdata_root) as connection:
        cursor = connection.execute(
            "INSERT INTO config_backups (status, file_name, size_bytes, included_file_count, "
            "created_at, completed_at, name, description, source_drone_id, source_drone_name, "
            "source_created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                STATUS_COMPLETE, file_name, int(size_bytes), 0, created_at, created_at,
                str(name or ""), str(description or ""), source_drone_id, source_drone_name, source_created_at,
            ),
        )
        backup_id = cursor.lastrowid
    return get(settings, backup_id)


def get(settings: Any, backup_id: int) -> Optional[dict]:
    with _open(settings.userdata_root) as connection:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM config_backups WHERE id = ?", (int(backup_id),)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_by_file_name(settings: Any, file_name: str) -> Optional[dict]:
    with _open(settings.userdata_root) as connection:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM config_backups WHERE file_name = ?", (str(file_name or ""),)
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_all(settings: Any, limit: int = 100) -> list[dict]:
    """Newest first. Config backups are a handful of admin-triggered rows, not an
    unbounded table, but this still caps the result set per the DB skill's
    pagination rule rather than assuming the table always stays small."""
    limit = max(1, min(int(limit or 100), 500))
    with _open(settings.userdata_root) as connection:
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM config_backups ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def list_complete_page(settings: Any, *, query: str = "", limit: int = 500, offset: int = 0) -> dict:
    """Peer-inventory page: only "complete" backups are ever downloadable, and
    the query matches name/description/file_name (whatever a requester might
    search on) rather than needing separate free-text search plumbing."""
    limit = max(1, min(int(limit or 500), 2000))
    offset = max(0, int(offset or 0))
    query_clean = str(query or "").strip().lower()
    with _open(settings.userdata_root) as connection:
        if query_clean:
            like = f"%{query_clean}%"
            where = "WHERE status = ? AND (lower(name) LIKE ? OR lower(description) LIKE ? OR lower(file_name) LIKE ?)"
            params: tuple = (STATUS_COMPLETE, like, like, like)
        else:
            where = "WHERE status = ?"
            params = (STATUS_COMPLETE,)
        total = connection.execute(f"SELECT COUNT(*) FROM config_backups {where}", params).fetchone()[0]
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM config_backups {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return {"total": int(total), "limit": limit, "offset": offset, "items": [_row_to_dict(row) for row in rows]}


def delete(settings: Any, backup_id: int) -> bool:
    with _open(settings.userdata_root) as connection:
        cursor = connection.execute("DELETE FROM config_backups WHERE id = ?", (int(backup_id),))
        return cursor.rowcount > 0


def any_creating(settings: Any) -> bool:
    with _open(settings.userdata_root) as connection:
        row = connection.execute(
            "SELECT 1 FROM config_backups WHERE status = ? LIMIT 1", (STATUS_CREATING,)
        ).fetchone()
    return row is not None
