"""Per-track outcome tracking for the bulk MusicBrainz scrape job (see
``music_scrape_jobs.py`` for the job's own aggregate progress row).

Mirrors ``movie_scrape_job_items.py`` exactly in shape -- same "only the
latest run matters, no history" convention (``clear()`` wipes the table at
the start of every new run). Rows are still one-per-*track* even though the
bulk job itself groups by (artist, album) internally -- every track in a
group gets its own matched/skipped/failed outcome, since a release lookup
resolving successfully doesn't guarantee every on-disk track in that album
folder matches something in it.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

try:
    from .state_store import database_path as _state_database_path
    from .state_store import open_database as _open_state_database
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import open_database as _open_state_database  # type: ignore


STATUS_MATCHED = "matched"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


def _open(userdata_root) -> sqlite3.Connection:
    connection = _open_state_database(_state_database_path(userdata_root))
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS music_scrape_job_items ("
        "entry_key TEXT PRIMARY KEY, track_name TEXT NOT NULL DEFAULT '', "
        "file_path TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '')"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_music_scrape_job_items_status ON music_scrape_job_items(status)")
    connection.commit()


def clear(settings: Any) -> None:
    with _open(settings.userdata_root) as connection:
        connection.execute("DELETE FROM music_scrape_job_items")
        connection.commit()


def record(settings: Any, entry_key: str, track_name: str, file_path: str, status: str, reason: str = "") -> None:
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "INSERT INTO music_scrape_job_items (entry_key, track_name, file_path, status, reason) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(entry_key) DO UPDATE SET "
            "track_name=excluded.track_name, file_path=excluded.file_path, status=excluded.status, reason=excluded.reason",
            (entry_key, track_name or "", file_path or "", status, reason or ""),
        )
        connection.commit()


def list_by_status(settings: Any, status: str, *, limit: int = 200, offset: int = 0) -> dict:
    safe_limit = max(1, min(int(limit), 2000))
    safe_offset = max(0, int(offset))
    with _open(settings.userdata_root) as connection:
        total = int(
            connection.execute(
                "SELECT COUNT(*) FROM music_scrape_job_items WHERE status = ?", (status,)
            ).fetchone()[0]
        )
        rows = connection.execute(
            "SELECT entry_key, track_name, file_path, reason FROM music_scrape_job_items "
            "WHERE status = ? ORDER BY file_path COLLATE NOCASE LIMIT ? OFFSET ?",
            (status, safe_limit, safe_offset),
        ).fetchall()
    items = [{"entry_key": r[0], "track_name": r[1], "file_path": r[2], "reason": r[3]} for r in rows]
    return {"total": total, "limit": safe_limit, "offset": safe_offset, "items": items}


def entry_keys_by_status(settings: Any, status: str) -> list:
    with _open(settings.userdata_root) as connection:
        rows = connection.execute(
            "SELECT entry_key FROM music_scrape_job_items WHERE status = ?", (status,)
        ).fetchall()
    return [row[0] for row in rows]
