"""Relational audit trail + notifications inbox for the Drone's own local activity.

Two tables, deliberately kept separate despite being written together by
``insert_event()``:

* ``audit_log`` -- the permanent record. Feeds the SMTP digest poller
  (``device/smtp_manager.py``), which marks rows ``emailed_at`` once included
  in a sent digest. Never touched by a user clearing notifications.
* ``notifications`` -- the UI-facing inbox (bell icon dropdown). One row per
  audit event, linked via ``audit_log_id``, with its own ``read_at`` state.
  Rows here are hard-deleted when a user clears them -- that must never
  reach back into ``audit_log``, which is the trail the email pipeline
  depends on.

Same shared-sqlite-file convention as every other ``storage/*_store.py``
module (see ``saves_store.py`` for the cleanest example): one physical
database (``state_store.database_path``), each module owns its own tables
via its own idempotent ``_ensure_schema()``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from .state_store import database_path as _state_database_path
    from .state_store import open_database as _open_state_database
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import open_database as _open_state_database  # type: ignore


# Bounded retention: only ever removes already-emailed audit rows / already-read
# notifications -- never anything still pending delivery or unread. See
# prune_old_events().
AUDIT_RETENTION_DAYS = 180
AUDIT_RETENTION_MAX_ROWS = 5000
NOTIFICATION_RETENTION_DAYS = 90

DEFAULT_PAGE_LIMIT = 25
MAX_PAGE_LIMIT = 200


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _db_path(userdata_root: Path) -> Path:
    return _state_database_path(userdata_root)


def _open(userdata_root: Path) -> sqlite3.Connection:
    connection = _open_state_database(_db_path(userdata_root))
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS audit_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, title TEXT NOT NULL, "
        "message TEXT NOT NULL DEFAULT '', details TEXT, created_at TEXT NOT NULL, emailed_at TEXT)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_log_pending_email ON audit_log(event_type, emailed_at)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS notifications ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "audit_log_id INTEGER NOT NULL REFERENCES audit_log(id), "
        "event_type TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL, read_at TEXT)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications(created_at DESC)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_notifications_read_at ON notifications(read_at)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_audit_log_id ON notifications(audit_log_id)"
    )
    connection.commit()


def _row_to_audit_dict(row: tuple) -> dict:
    return {
        "id": row[0],
        "event_type": row[1],
        "title": row[2],
        "message": row[3],
        "details": json.loads(row[4]) if row[4] else None,
        "created_at": row[5],
        "emailed_at": row[6],
    }


def _row_to_notification_dict(row: tuple) -> dict:
    return {
        "id": row[0],
        "audit_log_id": row[1],
        "event_type": row[2],
        "title": row[3],
        "message": row[4],
        "created_at": row[5],
        "read_at": row[6],
        "read": row[6] is not None,
    }


def insert_event(
    settings: Any,
    event_type: str,
    title: str,
    message: str = "",
    details: Optional[dict] = None,
) -> dict:
    """Insert one audit_log row and its linked notifications row, atomically."""
    event_type = str(event_type or "").strip()
    if not event_type:
        raise ValueError("event_type is required")
    title = str(title or "").strip() or event_type
    message = str(message or "")
    created_at = _now()
    details_json = json.dumps(details, sort_keys=True, default=str) if details else None
    with _open(settings.userdata_root) as connection:
        cursor = connection.execute(
            "INSERT INTO audit_log (event_type, title, message, details, created_at, emailed_at) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (event_type, title, message, details_json, created_at),
        )
        audit_log_id = cursor.lastrowid
        notification_cursor = connection.execute(
            "INSERT INTO notifications (audit_log_id, event_type, title, message, created_at, read_at) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (audit_log_id, event_type, title, message, created_at),
        )
        notification_id = notification_cursor.lastrowid
    return {
        "id": notification_id,
        "audit_log_id": audit_log_id,
        "event_type": event_type,
        "title": title,
        "message": message,
        "created_at": created_at,
        "read_at": None,
        "read": False,
    }


def list_unsent_events(settings: Any, event_types: Iterable[str], limit: int = 200) -> list[dict]:
    """Audit rows not yet included in a digest, restricted to the given (enabled) types."""
    types = [str(t).strip() for t in event_types if str(t or "").strip()]
    if not types:
        return []
    placeholders = ",".join("?" for _ in types)
    with _open(settings.userdata_root) as connection:
        rows = connection.execute(
            f"SELECT id, event_type, title, message, details, created_at, emailed_at FROM audit_log "
            f"WHERE emailed_at IS NULL AND event_type IN ({placeholders}) "
            f"ORDER BY id ASC LIMIT ?",
            (*types, max(1, min(int(limit), 1000))),
        ).fetchall()
    return [_row_to_audit_dict(row) for row in rows]


def mark_events_emailed(settings: Any, ids: Iterable[int]) -> int:
    id_list = [int(i) for i in ids]
    if not id_list:
        return 0
    placeholders = ",".join("?" for _ in id_list)
    with _open(settings.userdata_root) as connection:
        cursor = connection.execute(
            f"UPDATE audit_log SET emailed_at = ? WHERE id IN ({placeholders}) AND emailed_at IS NULL",
            (_now(), *id_list),
        )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def list_notifications_page(
    settings: Any,
    *,
    before_id: Optional[int] = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    unread_only: bool = False,
) -> dict:
    """Keyset-paginated, newest-first."""
    limit = max(1, min(int(limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT))
    clauses = []
    params: list[Any] = []
    if before_id is not None:
        clauses.append("id < ?")
        params.append(int(before_id))
    if unread_only:
        clauses.append("read_at IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _open(settings.userdata_root) as connection:
        rows = connection.execute(
            f"SELECT id, audit_log_id, event_type, title, message, created_at, read_at "
            f"FROM notifications {where} ORDER BY id DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        unread_count = connection.execute(
            "SELECT COUNT(*) FROM notifications WHERE read_at IS NULL"
        ).fetchone()[0]
    has_more = len(rows) > limit
    items = [_row_to_notification_dict(row) for row in rows[:limit]]
    return {
        "items": items,
        "limit": limit,
        "has_more": has_more,
        "next_before_id": items[-1]["id"] if has_more and items else None,
        "unread_count": int(unread_count),
    }


def unread_notification_count(settings: Any) -> int:
    with _open(settings.userdata_root) as connection:
        row = connection.execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL").fetchone()
    return int(row[0]) if row else 0


def mark_notification_read(settings: Any, notification_id: int) -> bool:
    with _open(settings.userdata_root) as connection:
        cursor = connection.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
            (_now(), int(notification_id)),
        )
        return bool(cursor.rowcount)


def mark_all_notifications_read(settings: Any) -> int:
    with _open(settings.userdata_root) as connection:
        cursor = connection.execute("UPDATE notifications SET read_at = ? WHERE read_at IS NULL", (_now(),))
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def delete_notification(settings: Any, notification_id: int) -> bool:
    with _open(settings.userdata_root) as connection:
        cursor = connection.execute("DELETE FROM notifications WHERE id = ?", (int(notification_id),))
        return bool(cursor.rowcount)


def clear_notifications(settings: Any, *, only_read: bool = False) -> int:
    with _open(settings.userdata_root) as connection:
        if only_read:
            cursor = connection.execute("DELETE FROM notifications WHERE read_at IS NOT NULL")
        else:
            cursor = connection.execute("DELETE FROM notifications")
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def prune_old_events(settings: Any) -> dict:
    """Opportunistic retention, called once per digest-poller tick.

    Only ever removes already-emailed audit_log rows / already-read
    notifications -- unsent audit rows and unread notifications are never
    pruned automatically, regardless of age.
    """
    with _open(settings.userdata_root) as connection:
        audit_deleted = connection.execute(
            "DELETE FROM audit_log WHERE emailed_at IS NOT NULL AND created_at < ?",
            (_iso_days_ago(AUDIT_RETENTION_DAYS),),
        ).rowcount
        connection.execute(
            "DELETE FROM audit_log WHERE emailed_at IS NOT NULL AND id NOT IN ("
            "SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)",
            (AUDIT_RETENTION_MAX_ROWS,),
        )
        notifications_deleted = connection.execute(
            "DELETE FROM notifications WHERE read_at IS NOT NULL AND created_at < ?",
            (_iso_days_ago(NOTIFICATION_RETENTION_DAYS),),
        ).rowcount
    return {
        "audit_rows_pruned": max(0, audit_deleted or 0),
        "notifications_pruned": max(0, notifications_deleted or 0),
    }
