"""Per-movie outcome tracking for the bulk TMDb scrape job (see
``movie_scrape_jobs.py`` for the job's own aggregate progress row).

The aggregate ``matched_count``/``skipped_count``/``failed_count`` on the job
row answer "how many failed" but not "*which* ones, and why" -- this table
answers that, one row per movie in the *most recent* run (same "only the
latest run matters, no history" convention ``movie_scrape_jobs`` already
uses: ``clear()`` wipes the table at the start of every new run rather than
accumulating rows across runs). It backs the admin UI's clickable
matched/skipped/failed breakdown and the "retry failed" action, which needs
the exact set of entry_keys that failed and a human-readable reason for each
(e.g. "no TMDb results for this title", "TMDb is rate-limiting this Drone").
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
        "CREATE TABLE IF NOT EXISTS movie_scrape_job_items ("
        "entry_key TEXT PRIMARY KEY, movie_name TEXT NOT NULL DEFAULT '', "
        "file_path TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '')"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_movie_scrape_job_items_status ON movie_scrape_job_items(status)")
    connection.commit()


def clear(settings: Any) -> None:
    """Wipe every row -- called once at the start of a new bulk run, before
    any candidate is processed, so a retry-scoped re-run (see
    ``metadata_manager.retry_bulk_scrape_items``) doesn't inherit stale rows
    for movies it isn't touching this time."""
    with _open(settings.userdata_root) as connection:
        connection.execute("DELETE FROM movie_scrape_job_items")
        connection.commit()


def record(settings: Any, entry_key: str, movie_name: str, file_path: str, status: str, reason: str = "") -> None:
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "INSERT INTO movie_scrape_job_items (entry_key, movie_name, file_path, status, reason) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(entry_key) DO UPDATE SET "
            "movie_name=excluded.movie_name, file_path=excluded.file_path, status=excluded.status, reason=excluded.reason",
            (entry_key, movie_name or "", file_path or "", status, reason or ""),
        )
        connection.commit()


def list_by_status(settings: Any, status: str, *, limit: int = 200, offset: int = 0) -> dict:
    safe_limit = max(1, min(int(limit), 2000))
    safe_offset = max(0, int(offset))
    with _open(settings.userdata_root) as connection:
        total = int(
            connection.execute(
                "SELECT COUNT(*) FROM movie_scrape_job_items WHERE status = ?", (status,)
            ).fetchone()[0]
        )
        rows = connection.execute(
            "SELECT entry_key, movie_name, file_path, reason FROM movie_scrape_job_items "
            "WHERE status = ? ORDER BY file_path COLLATE NOCASE LIMIT ? OFFSET ?",
            (status, safe_limit, safe_offset),
        ).fetchall()
    items = [{"entry_key": r[0], "movie_name": r[1], "file_path": r[2], "reason": r[3]} for r in rows]
    return {"total": total, "limit": safe_limit, "offset": safe_offset, "items": items}


def entry_keys_by_status(settings: Any, status: str) -> list:
    """Every entry_key for a status, unpaged -- used by "retry all failed"
    (needs the whole set, not one page of it)."""
    with _open(settings.userdata_root) as connection:
        rows = connection.execute(
            "SELECT entry_key FROM movie_scrape_job_items WHERE status = ?", (status,)
        ).fetchall()
    return [row[0] for row in rows]
