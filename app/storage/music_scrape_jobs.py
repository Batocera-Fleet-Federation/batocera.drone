"""Progress tracking for the "scrape all music" bulk admin action (the Music
tab on the Artwork admin page).

Mirrors ``movie_scrape_jobs.py`` exactly in shape -- see that module's
docstring for the "SQLite row, not an in-process flag, is the source of
truth" rationale. ``current_music`` holds a human-readable label for
whatever's being scraped right now -- an "artist – album" string for a
release group, or a bare track name for a singles-bucket candidate (see
``music/metadata_manager.py``'s bulk job), not a single track_name the way
movies' ``current_movie`` always is.
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
        "CREATE TABLE IF NOT EXISTS music_scrape_jobs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "status TEXT NOT NULL DEFAULT 'running', "
        "rescan_all INTEGER NOT NULL DEFAULT 0, "
        "total INTEGER NOT NULL DEFAULT 0, "
        "processed INTEGER NOT NULL DEFAULT 0, "
        "matched_count INTEGER NOT NULL DEFAULT 0, "
        "skipped_count INTEGER NOT NULL DEFAULT 0, "
        "failed_count INTEGER NOT NULL DEFAULT 0, "
        "current_music TEXT NOT NULL DEFAULT '', "
        "error_message TEXT, "
        "started_at TEXT NOT NULL, "
        "completed_at TEXT)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_music_scrape_jobs_status ON music_scrape_jobs(status)")
    connection.commit()


_COLUMNS = (
    "id, status, rescan_all, total, processed, matched_count, skipped_count, "
    "failed_count, current_music, error_message, started_at, completed_at"
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
        "current_music": row[8] or "",
        "error_message": row[9],
        "started_at": row[10],
        "completed_at": row[11],
    }


def create_running(settings: Any, *, rescan_all: bool, total: int) -> dict:
    started_at = _now()
    with _open(settings.userdata_root) as connection:
        cursor = connection.execute(
            "INSERT INTO music_scrape_jobs (status, rescan_all, total, started_at) VALUES (?, ?, ?, ?)",
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
        "current_music": "",
        "error_message": None,
        "started_at": started_at,
        "completed_at": None,
    }


def update_progress(
    settings: Any,
    job_id: int,
    *,
    processed: int,
    current_music: str,
    matched_count: int,
    skipped_count: int,
    failed_count: int,
) -> None:
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "UPDATE music_scrape_jobs SET processed = ?, current_music = ?, matched_count = ?, "
            "skipped_count = ?, failed_count = ? WHERE id = ?",
            (int(processed), str(current_music or ""), int(matched_count), int(skipped_count), int(failed_count), job_id),
        )


def mark_complete(settings: Any, job_id: int) -> None:
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "UPDATE music_scrape_jobs SET status = ?, current_music = '', completed_at = ? WHERE id = ?",
            (STATUS_COMPLETE, _now(), job_id),
        )


def mark_error(settings: Any, job_id: int, message: str) -> None:
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "UPDATE music_scrape_jobs SET status = ?, error_message = ?, completed_at = ? WHERE id = ?",
            (STATUS_ERROR, str(message or "scrape failed"), _now(), job_id),
        )


def latest(settings: Any) -> Optional[dict]:
    with _open(settings.userdata_root) as connection:
        row = connection.execute(f"SELECT {_COLUMNS} FROM music_scrape_jobs ORDER BY id DESC LIMIT 1").fetchone()
    return _row_to_dict(row) if row else None


def any_running(settings: Any) -> bool:
    with _open(settings.userdata_root) as connection:
        row = connection.execute("SELECT 1 FROM music_scrape_jobs WHERE status = ? LIMIT 1", (STATUS_RUNNING,)).fetchone()
    return row is not None
