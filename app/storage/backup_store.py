"""Metadata for categorized backups (``backups/``), in the shared state database.

Archive bytes and the full per-file manifest live on disk under
``backups.service.backups_directory()``; each row carries what listing needs
(categories with counts, source Drone, compatibility facts) so neither the admin
page nor a peer's listing request ever opens an archive.

``backup_id`` is the backup's global identity: it is minted where the backup was
created and kept when a peer downloads it, so the same backup is recognized on every
Drone. ``origin`` says whether this Drone built it (``local``) or pulled it
(``peer``); ``kind`` separates user backups from automatic pre-restore snapshots,
which are never offered to peers.

The retired single-tarball ``config_backups`` table is intentionally left alone
(schema is never dropped in place); nothing reads it any more.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

try:
    from .state_store import database_path as _state_database_path
    from .state_store import open_database as _open_state_database
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import open_database as _open_state_database  # type: ignore

STATUS_CREATING = "creating"
STATUS_COMPLETE = "complete"
STATUS_ERROR = "error"

_COLUMNS = (
    "backup_id", "kind", "status", "name", "description", "created_at", "added_at", "completed_at",
    "size_bytes", "archive_sha256", "file_count", "content_bytes", "categories_json", "source_json",
    "origin", "downloaded_from_id", "downloaded_from_name", "snapshot_of_json", "skipped_count",
    "skipped_bytes", "error_message",
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _open(userdata_root: Path) -> sqlite3.Connection:
    connection = _open_state_database(_state_database_path(Path(userdata_root)))
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS drone_backups ("
        "backup_id TEXT PRIMARY KEY, "
        "kind TEXT NOT NULL DEFAULT 'backup', "
        "status TEXT NOT NULL DEFAULT 'creating', "
        "name TEXT NOT NULL DEFAULT '', "
        "description TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL, "
        "added_at TEXT NOT NULL, "
        "completed_at TEXT, "
        "size_bytes INTEGER NOT NULL DEFAULT 0, "
        "archive_sha256 TEXT, "
        "file_count INTEGER NOT NULL DEFAULT 0, "
        "content_bytes INTEGER NOT NULL DEFAULT 0, "
        "categories_json TEXT NOT NULL DEFAULT '[]', "
        "source_json TEXT NOT NULL DEFAULT '{}', "
        "origin TEXT NOT NULL DEFAULT 'local', "
        "downloaded_from_id TEXT, "
        "downloaded_from_name TEXT, "
        "snapshot_of_json TEXT, "
        "skipped_count INTEGER NOT NULL DEFAULT 0, "
        "skipped_bytes INTEGER NOT NULL DEFAULT 0, "
        "error_message TEXT)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_drone_backups_added ON drone_backups(added_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_drone_backups_kind_status ON drone_backups(kind, status)")
    connection.commit()


def _json(value: Optional[str], default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def _row(row: tuple) -> dict:
    data = dict(zip(_COLUMNS, row))
    data["categories"] = _json(data.pop("categories_json"), [])
    data["source"] = _json(data.pop("source_json"), {})
    data["snapshot_of"] = _json(data.pop("snapshot_of_json"), None)
    data["is_local"] = data["origin"] == "local"
    return data


def _select(where: str = "", params: tuple = ()) -> str:
    return f"SELECT {', '.join(_COLUMNS)} FROM drone_backups {where}"


def create_pending(settings: Any, backup_id: str, *, kind: str, name: str, description: str,
                   snapshot_of: Optional[dict] = None) -> dict:
    now = _now()
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "INSERT INTO drone_backups (backup_id, kind, status, name, description, created_at, added_at, "
            "origin, snapshot_of_json) VALUES (?, ?, ?, ?, ?, ?, ?, 'local', ?)",
            (backup_id, kind, STATUS_CREATING, name, description, now, now,
             json.dumps(snapshot_of) if snapshot_of else None),
        )
    return get(settings, backup_id) or {}


def _manifest_columns(manifest: dict, size_bytes: int, archive_sha256: str) -> dict:
    return {
        "size_bytes": int(size_bytes),
        "archive_sha256": archive_sha256,
        "file_count": int(manifest.get("file_count") or 0),
        "content_bytes": int(manifest.get("size_bytes") or 0),
        "categories_json": json.dumps(manifest.get("categories") or []),
        "source_json": json.dumps(manifest.get("source") or {}),
        "skipped_count": int(manifest.get("skipped_count") or 0),
        "skipped_bytes": int(manifest.get("skipped_bytes") or 0),
    }


def mark_complete(settings: Any, backup_id: str, manifest: dict, *, size_bytes: int, archive_sha256: str) -> None:
    columns = _manifest_columns(manifest, size_bytes, archive_sha256)
    columns.update(status=STATUS_COMPLETE, completed_at=_now(), created_at=manifest.get("created_at") or _now(),
                   error_message=None)
    assignments = ", ".join(f"{key} = ?" for key in columns)
    with _open(settings.userdata_root) as connection:
        connection.execute(f"UPDATE drone_backups SET {assignments} WHERE backup_id = ?",
                           (*columns.values(), backup_id))


def mark_error(settings: Any, backup_id: str, message: str) -> None:
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "UPDATE drone_backups SET status = ?, error_message = ?, completed_at = ? WHERE backup_id = ?",
            (STATUS_ERROR, str(message)[:2000], _now(), backup_id),
        )


def record_downloaded(settings: Any, manifest: dict, *, size_bytes: int, archive_sha256: str,
                      from_id: str, from_name: str) -> dict:
    columns = _manifest_columns(manifest, size_bytes, archive_sha256)
    now = _now()
    columns.update(
        backup_id=manifest["backup_id"], kind="backup", status=STATUS_COMPLETE, name=manifest.get("name") or "",
        description=manifest.get("description") or "", created_at=manifest.get("created_at") or now,
        added_at=now, completed_at=now, origin="peer", downloaded_from_id=from_id, downloaded_from_name=from_name,
    )
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    with _open(settings.userdata_root) as connection:
        connection.execute(f"INSERT OR REPLACE INTO drone_backups ({names}) VALUES ({marks})", tuple(columns.values()))
    return get(settings, manifest["backup_id"]) or {}


def update_text(settings: Any, backup_id: str, *, name: str, description: str) -> Optional[dict]:
    with _open(settings.userdata_root) as connection:
        connection.execute("UPDATE drone_backups SET name = ?, description = ? WHERE backup_id = ?",
                           (name, description, backup_id))
    return get(settings, backup_id)


def get(settings: Any, backup_id: str) -> Optional[dict]:
    with _open(settings.userdata_root) as connection:
        row = connection.execute(_select("WHERE backup_id = ?"), (backup_id,)).fetchone()
    return _row(row) if row else None


def list_rows(settings: Any, *, kind: Optional[str] = None, complete_only: bool = False) -> List[dict]:
    clauses, params = [], []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if complete_only:
        clauses.append("status = ?")
        params.append(STATUS_COMPLETE)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _open(settings.userdata_root) as connection:
        rows = connection.execute(_select(where) + " ORDER BY added_at DESC, created_at DESC", tuple(params)).fetchall()
    return [_row(row) for row in rows]


def creating_ids(settings: Any) -> List[str]:
    with _open(settings.userdata_root) as connection:
        rows = connection.execute("SELECT backup_id FROM drone_backups WHERE status = ?", (STATUS_CREATING,)).fetchall()
    return [row[0] for row in rows]


def delete(settings: Any, backup_id: str) -> None:
    with _open(settings.userdata_root) as connection:
        connection.execute("DELETE FROM drone_backups WHERE backup_id = ?", (backup_id,))
