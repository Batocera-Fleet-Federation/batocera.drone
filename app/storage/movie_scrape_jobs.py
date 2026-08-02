"""Progress tracking for the "scrape all movies" bulk admin action (the new
Movies tab on the Artwork admin page).

Same shape as ``config_backup_store.py`` (the closest existing precedent for
"click a button, kick off a slow one-shot background job, poll a status
endpoint"): a SQLite row -- not an in-process flag -- is the single source of
truth for whether a job is currently running, so it stays correct across
process restarts and doesn't leak state between tests. Unlike config
backups, there's nothing here worth listing historically (a bulk scrape run
isn't a downloadable artifact) -- callers only ever care about the most
recent job, so this module deliberately has no ``list_all``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

try:
    from .state_store import database_path as _state_database_path
    from .state_store import open_database as _open_state_database
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import open_database as _open_state_database  # type: ignore


STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_ERROR = "error"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _open(userdata_root) -> sqlite3.Connection:
    connection = _open_state_database(_state_database_path(userdata_root))
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS movie_scrape_jobs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "status TEXT NOT NULL DEFAULT 'running', "
        "rescan_all INTEGER NOT NULL DEFAULT 0, "
        "total INTEGER NOT NULL DEFAULT 0, "
        "processed INTEGER NOT NULL DEFAULT 0, "
        "matched_count INTEGER NOT NULL DEFAULT 0, "
        "skipped_count INTEGER NOT NULL DEFAULT 0, "
        "failed_count INTEGER NOT NULL DEFAULT 0, "
        "current_movie TEXT NOT NULL DEFAULT '', "
        "error_message TEXT, "
        "started_at TEXT NOT NULL, "
        "completed_at TEXT)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_movie_scrape_jobs_status ON movie_scrape_jobs(status)")
    connection.commit()


_COLUMNS = (
    "id, status, rescan_all, total, processed, matched_count, skipped_count, "
    "failed_count, current_movie, error_message, started_at, completed_at"
)


def _row_to_dict(row: tuple) -> dict:
    return {
        "id": row[0],
        "status": row[1],
        "rescan_all": bool(row[2]),
        "total": row[3],
        "processed": row[4],
        "matched_count": row[5],
        "skipped_count": row[6],
        "failed_count": row[7],
        "current_movie": row[8] or "",
        "error_message": row[9],
        "started_at": row[10],
        "completed_at": row[11],
    }


def create_running(settings: Any, *, rescan_all: bool, total: int) -> dict:
    """Insert the "running" row a background job will update as it goes."""
    started_at = _now()
    with _open(settings.userdata_root) as connection:
        cursor = connection.execute(
            "INSERT INTO movie_scrape_jobs (status, rescan_all, total, started_at) VALUES (?, ?, ?, ?)",
            (STATUS_RUNNING, 1 if rescan_all else 0, int(total), started_at),
        )
        job_id = cursor.lastrowid
    return {
        "id": job_id,
        "status": STATUS_RUNNING,
        "rescan_all": bool(rescan_all),
        "total": int(total),
        "processed": 0,
        "matched_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
        "current_movie": "",
        "error_message": None,
        "started_at": started_at,
        "completed_at": None,
    }


def update_progress(
    settings: Any,
    job_id: int,
    *,
    processed: int,
    current_movie: str,
    matched_count: int,
    skipped_count: int,
    failed_count: int,
) -> None:
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "UPDATE movie_scrape_jobs SET processed = ?, current_movie = ?, matched_count = ?, "
            "skipped_count = ?, failed_count = ? WHERE id = ?",
            (int(processed), str(current_movie or ""), int(matched_count), int(skipped_count), int(failed_count), job_id),
        )


def mark_complete(settings: Any, job_id: int) -> None:
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "UPDATE movie_scrape_jobs SET status = ?, current_movie = '', completed_at = ? WHERE id = ?",
            (STATUS_COMPLETE, _now(), job_id),
        )


def mark_error(settings: Any, job_id: int, message: str) -> None:
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "UPDATE movie_scrape_jobs SET status = ?, error_message = ?, completed_at = ? WHERE id = ?",
            (STATUS_ERROR, str(message or "scrape failed"), _now(), job_id),
        )


def latest(settings: Any) -> Optional[dict]:
    with _open(settings.userdata_root) as connection:
        row = connection.execute(f"SELECT {_COLUMNS} FROM movie_scrape_jobs ORDER BY id DESC LIMIT 1").fetchone()
    return _row_to_dict(row) if row else None


def any_running(settings: Any) -> bool:
    with _open(settings.userdata_root) as connection:
        row = connection.execute("SELECT 1 FROM movie_scrape_jobs WHERE status = ? LIMIT 1", (STATUS_RUNNING,)).fetchone()
    return row is not None
