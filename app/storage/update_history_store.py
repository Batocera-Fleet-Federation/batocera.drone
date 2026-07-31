"""Relational history of Drone app self-updates, for the System Info admin page.

One row per completed update (manual "Update Drone" click or the auto-update
poller), written from the single choke point both paths share
(``common/self_update.py``'s ``_download_latest_drone_app``). Deliberately
separate from ``audit_store.py``'s ``drone_updated`` event: the audit trail
is a generic, uniformly-shaped log of many different event types shown in the
notifications bell, while this table's whole purpose is to carry a specific,
richer payload (the release notes text, a link to the release) meant for its
own dedicated System Info section, not a one-line inbox entry.

Same shared-sqlite-file convention as every other ``storage/*_store.py``
module (see ``saves_store.py`` for the cleanest example): one physical
database (``state_store.database_path``), this module owns its own table via
its own idempotent ``_ensure_schema()``.
"""

from __future__ import annotations

import logging
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

_LOG = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _db_path(userdata_root: Path) -> Path:
    return _state_database_path(userdata_root)


def _open(userdata_root: Path) -> sqlite3.Connection:
    connection = _open_state_database(_db_path(userdata_root))
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS drone_update_history ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "previous_version TEXT, "
        "version TEXT NOT NULL, "
        "release_url TEXT, "
        "release_notes TEXT, "
        "applied_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_drone_update_history_applied_at ON drone_update_history(applied_at DESC)"
    )
    connection.commit()


def _row_to_dict(row: tuple) -> dict:
    return {
        "id": row[0],
        "previous_version": row[1],
        "version": row[2],
        "release_url": row[3],
        "release_notes": row[4],
        "applied_at": row[5],
    }


_COLUMNS = "id, previous_version, version, release_url, release_notes, applied_at"


def record_update(
    settings: Any,
    *,
    version: str,
    previous_version: str = "",
    release_url: str = "",
    release_notes: str = "",
) -> Optional[dict]:
    """Log a completed self-update. Never raises -- a storage hiccup here
    must not surface as an "update failed" to the caller, mirroring
    ``notifications.record_event()``'s leaf-module contract."""
    try:
        applied_at = _now()
        with _open(settings.userdata_root) as connection:
            cursor = connection.execute(
                "INSERT INTO drone_update_history "
                "(previous_version, version, release_url, release_notes, applied_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(previous_version or ""), str(version or ""), str(release_url or ""), str(release_notes or ""), applied_at),
            )
            row_id = cursor.lastrowid
        return {
            "id": row_id,
            "previous_version": str(previous_version or ""),
            "version": str(version or ""),
            "release_url": str(release_url or ""),
            "release_notes": str(release_notes or ""),
            "applied_at": applied_at,
        }
    except Exception:
        _LOG.exception("failed to record drone update history version=%s", version)
        return None


def list_updates(settings: Any, limit: int = DEFAULT_LIST_LIMIT) -> list:
    """Newest first."""
    limit = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
    with _open(settings.userdata_root) as connection:
        rows = connection.execute(
            f"SELECT {_COLUMNS} FROM drone_update_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(row) for row in rows]
