"""Persistent outbound-email queue owned by the Drone API worker.

UI clients only enqueue work through API handlers.  The SMTP worker is the
only consumer and the only code path allowed to open an SMTP connection.
Rows are idempotent by source Drone + source job ID so a lost peer response
cannot produce a second email.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from .state_store import database_path as _state_database_path
    from .state_store import open_database as _open_state_database
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import open_database as _open_state_database  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _open(userdata_root: Path) -> sqlite3.Connection:
    connection = _open_state_database(_state_database_path(userdata_root))
    connection.execute(
        "CREATE TABLE IF NOT EXISTS outbound_mail_queue ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "source_drone_id TEXT NOT NULL, source_job_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, "
        "attachment_path TEXT, attachment_filename TEXT, metadata TEXT, "
        "status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
        "next_attempt_at TEXT, last_error TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sent_at TEXT, "
        "UNIQUE(source_drone_id, source_job_id))"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbound_mail_ready "
        "ON outbound_mail_queue(status, next_attempt_at, id)"
    )
    connection.commit()
    return connection


def _row(row: tuple) -> dict:
    return {
        "id": int(row[0]),
        "source_drone_id": row[1],
        "source_job_id": row[2],
        "kind": row[3],
        "subject": row[4],
        "body": row[5],
        "attachment_path": row[6] or "",
        "attachment_filename": row[7] or "",
        "metadata": json.loads(row[8]) if row[8] else {},
        "status": row[9],
        "attempts": int(row[10] or 0),
        "next_attempt_at": row[11],
        "last_error": row[12] or "",
        "created_at": row[13],
        "updated_at": row[14],
        "sent_at": row[15],
    }


_SELECT = (
    "SELECT id, source_drone_id, source_job_id, kind, subject, body, "
    "attachment_path, attachment_filename, metadata, status, attempts, "
    "next_attempt_at, last_error, created_at, updated_at, sent_at "
    "FROM outbound_mail_queue"
)


def enqueue(
    settings: Any,
    *,
    kind: str,
    subject: str,
    body: str,
    source_drone_id: Optional[str] = None,
    source_job_id: Optional[str] = None,
    attachment_path: Optional[Path] = None,
    attachment_filename: str = "",
    metadata: Optional[dict] = None,
) -> dict:
    source_drone_id = str(source_drone_id or settings.device_id or "local").strip()
    source_job_id = str(source_job_id or uuid.uuid4()).strip()
    kind = str(kind or "message").strip()
    if not source_drone_id or not source_job_id or not kind:
        raise ValueError("source_drone_id, source_job_id, and kind are required")
    created_at = _now()
    metadata_json = json.dumps(metadata or {}, sort_keys=True, default=str)
    path_value = str(attachment_path) if attachment_path else ""
    with _open(settings.userdata_root) as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO outbound_mail_queue "
            "(source_drone_id, source_job_id, kind, subject, body, attachment_path, "
            "attachment_filename, metadata, status, attempts, next_attempt_at, "
            "last_error, created_at, updated_at, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, NULL, '', ?, ?, NULL)",
            (
                source_drone_id,
                source_job_id,
                kind,
                str(subject or "")[:1000],
                str(body or "")[:100000],
                path_value,
                str(attachment_filename or "")[:512],
                metadata_json,
                created_at,
                created_at,
            ),
        )
        duplicate = not bool(cursor.rowcount)
        row = connection.execute(
            f"{_SELECT} WHERE source_drone_id = ? AND source_job_id = ?",
            (source_drone_id, source_job_id),
        ).fetchone()
    result = _row(row)
    result["duplicate"] = duplicate
    return result


def ready(settings: Any, limit: int = 10) -> list[dict]:
    now = _now()
    with _open(settings.userdata_root) as connection:
        rows = connection.execute(
            f"{_SELECT} WHERE status IN ('queued', 'error') "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT ?",
            (now, max(1, min(100, int(limit)))),
        ).fetchall()
    return [_row(row) for row in rows]


def pending(settings: Any, limit: int = 100) -> list[dict]:
    with _open(settings.userdata_root) as connection:
        rows = connection.execute(
            f"{_SELECT} WHERE status IN ('queued', 'error') ORDER BY id LIMIT ?",
            (max(1, min(500, int(limit))),),
        ).fetchall()
    return [_row(row) for row in rows]


def has_pending_kind(settings: Any, kind: str) -> bool:
    with _open(settings.userdata_root) as connection:
        row = connection.execute(
            "SELECT 1 FROM outbound_mail_queue WHERE kind = ? "
            "AND status IN ('queued', 'error') LIMIT 1",
            (str(kind),),
        ).fetchone()
    return row is not None


def mark_sent(settings: Any, job_id: int) -> None:
    now = _now()
    with _open(settings.userdata_root) as connection:
        connection.execute(
            "UPDATE outbound_mail_queue SET status = 'sent', sent_at = ?, "
            "updated_at = ?, last_error = '', next_attempt_at = NULL WHERE id = ?",
            (now, now, int(job_id)),
        )


def mark_relayed(settings: Any, job_ids: list[int]) -> None:
    ids = [int(value) for value in job_ids]
    if not ids:
        return
    now = _now()
    placeholders = ",".join("?" for _ in ids)
    with _open(settings.userdata_root) as connection:
        connection.execute(
            f"UPDATE outbound_mail_queue SET status = 'relayed', sent_at = ?, "
            f"updated_at = ?, last_error = '', next_attempt_at = NULL WHERE id IN ({placeholders})",
            (now, now, *ids),
        )


def mark_failed(settings: Any, job_id: int, error: str) -> None:
    with _open(settings.userdata_root) as connection:
        row = connection.execute(
            "SELECT attempts FROM outbound_mail_queue WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        attempts = int((row or [0])[0] or 0) + 1
        delay = min(3600, 60 * (2 ** min(5, attempts - 1)))
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        connection.execute(
            "UPDATE outbound_mail_queue SET status = 'error', attempts = ?, "
            "next_attempt_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (
                attempts,
                (now_dt + timedelta(seconds=delay)).isoformat(),
                str(error or "unknown mail delivery error")[:4000],
                now_dt.isoformat(),
                int(job_id),
            ),
        )
