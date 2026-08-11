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
STATUS_STOPPED = "stopped"

# A "running" job with no progress update in this long is treated as
# orphaned/dead, not genuinely still working -- see reconcile_if_stale.
# Generous on purpose: even a pathologically large group (dozens of tracks,
# each candidate potentially eating up to MUSICBRAINZ_MAX_RETRY_DELAY_SECONDS
# per retry, up to a few retries) should still tick well inside this window
# under any real MusicBrainz-availability condition; only a genuine hang
# (e.g. the uncapped-Retry-After bug this replaces) goes this long silent.
STALE_AFTER_SECONDS = 600


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
    # Added after the initial release -- _ensure_column so an already-deployed
    # Drone upgrades in place (see the drone-db-management skill: never bump
    # applied schema in place, add columns idempotently).
    _ensure_column(connection, "music_scrape_jobs", "stop_requested", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "music_scrape_jobs", "updated_at", "TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_music_scrape_jobs_status ON music_scrape_jobs(status)")
    connection.commit()


_COLUMNS = (
    "id, status, rescan_all, total, processed, matched_count, skipped_count, "
    "failed_count, current_music, error_message, started_at, completed_at, stop_requested, updated_at"
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
        "stop_requested": bool(row[12]),
        "updated_at": row[13] or row[10],
    }


def create_running(settings: Any, *, rescan_all: bool, total: int) -> dict:
    started_at = _now()
    with _open(settings.userdata_root) as connection:
        cursor = connection.execute(
            "INSERT INTO music_scrape_jobs (status, rescan_all, total, started_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (STATUS_RUNNING, 1 if rescan_all else 0, int(total), started_at, started_at),
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
        "stop_requested": False,
        "updated_at": started_at,
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
    # updated_at is this job's heartbeat -- reconcile_if_stale uses it to
    # tell "still genuinely working" from "orphaned" (see that function).
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "UPDATE music_scrape_jobs SET processed = ?, current_music = ?, matched_count = ?, "
            "skipped_count = ?, failed_count = ?, updated_at = ? WHERE id = ?",
            (int(processed), str(current_music or ""), int(matched_count), int(skipped_count), int(failed_count), _now(), job_id),
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


def mark_stopped(settings: Any, job_id: int) -> None:
    """A user-requested stop, not a failure -- the run simply ends early with
    whatever it had matched/skipped/failed so far; everything not yet reached
    is left untouched (no job_items recorded for it), unlike the
    provider-unavailable early-stop path which marks every remaining
    candidate failed since those genuinely can't succeed."""
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "UPDATE music_scrape_jobs SET status = ?, current_music = '', completed_at = ? WHERE id = ?",
            (STATUS_STOPPED, _now(), job_id),
        )


def request_stop(settings: Any, job_id: int) -> None:
    """Flag a running job to stop at its next per-candidate check
    (``is_stop_requested``) -- the SQLite row is the signal, not an
    in-process flag/Event, so it works the same whether the request lands on
    the thread that's actually running the job or a different request
    handler thread entirely."""
    with _open(settings.userdata_root) as connection:
        connection.execute("UPDATE music_scrape_jobs SET stop_requested = 1 WHERE id = ?", (job_id,))


def is_stop_requested(settings: Any, job_id: int) -> bool:
    with _open(settings.userdata_root) as connection:
        row = connection.execute("SELECT stop_requested FROM music_scrape_jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row[0])


def reconcile_if_stale(settings: Any) -> None:
    """A "running" job with no heartbeat (see ``update_progress``) in
    ``STALE_AFTER_SECONDS`` is treated as orphaned -- its background thread
    is presumed dead (a hang, a killed process that never reached a
    terminal status, ...) rather than genuinely still working -- and gets
    marked ``STATUS_ERROR`` so ``any_running()`` stops blocking a fresh
    scrape and the status view reflects reality instead of "running"
    forever. Called from both ``any_running()`` and ``latest()`` (not just
    ``start_bulk_scrape``) so every entry point -- the status endpoint the
    admin UI polls every 2s, clicking Stop, or clicking Start -- self-heals
    on its own within one call, not just whichever specific path someone
    happened to think to guard.

    Exists because of a real live incident: an uncapped ``Retry-After``
    sleep (now capped, see ``MUSICBRAINZ_MAX_RETRY_DELAY_SECONDS``) left a
    job's background thread blocked for 5+ hours. ``stop_requested`` was
    set correctly, but the thread never returned to the top of its loop to
    see it, and there was no way to start a new scrape short of hand-editing
    the job row's status on the live device.
    """
    with _open(settings.userdata_root) as connection:
        row = connection.execute(
            "SELECT id, updated_at, started_at FROM music_scrape_jobs WHERE status = ? ORDER BY id DESC LIMIT 1",
            (STATUS_RUNNING,),
        ).fetchone()
        if not row:
            return
        job_id, updated_at, started_at = row
        heartbeat = updated_at or started_at
        try:
            age_seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds()
        except (TypeError, ValueError):
            return
        if age_seconds <= STALE_AFTER_SECONDS:
            return
        connection.execute(
            "UPDATE music_scrape_jobs SET status = ?, error_message = ?, completed_at = ? WHERE id = ? AND status = ?",
            (
                STATUS_ERROR,
                f"Scrape appears stalled (no progress for over {STALE_AFTER_SECONDS // 60} minutes) -- marked as failed so a new scrape can start.",
                _now(),
                job_id,
                STATUS_RUNNING,
            ),
        )
        connection.commit()


def latest(settings: Any) -> Optional[dict]:
    reconcile_if_stale(settings)
    with _open(settings.userdata_root) as connection:
        row = connection.execute(f"SELECT {_COLUMNS} FROM music_scrape_jobs ORDER BY id DESC LIMIT 1").fetchone()
    return _row_to_dict(row) if row else None


def any_running(settings: Any) -> bool:
    reconcile_if_stale(settings)
    with _open(settings.userdata_root) as connection:
        row = connection.execute("SELECT 1 FROM music_scrape_jobs WHERE status = ? LIMIT 1", (STATUS_RUNNING,)).fetchone()
    return row is not None
