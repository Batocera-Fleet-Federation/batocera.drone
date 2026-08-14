"""Short-lived, single-track-scoped tokens that authorize streaming one
music track without the browser's session cookie.

Near-verbatim mirror of ``movie_cast_tokens.py`` (separate table, separate
module -- same "mirror, don't share" convention every other movies/music
pair in this app already follows) -- exists for the identical reason: an
AirPlay receiver fetches the audio URL itself, directly, with no browser in
the loop, so it can't carry the session cookie the rest of this app's
browsing surface requires, and it can't click through this Drone's
self-signed HTTPS cert either (see ``drone_api.py``'s ``_CastHttpHandler``,
the plain-HTTP listener these tokens gate). A token is minted by an
already-authenticated request (``handlers_music.py``'s
``_handle_music_cast_token_create``) and is good for exactly one track.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from .state_store import database_path as _state_database_path
    from .state_store import open_database as _open_state_database
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import open_database as _open_state_database  # type: ignore


# A single track is a few minutes, but a listening session isn't -- the
# player bar can sit on one track for a long time (paused, or just idle
# between songs), same generous-on-purpose reasoning as
# movie_cast_tokens.DEFAULT_TTL_SECONDS.
DEFAULT_TTL_SECONDS = 12 * 60 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _open(userdata_root) -> sqlite3.Connection:
    connection = _open_state_database(_state_database_path(userdata_root))
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS music_cast_tokens ("
        "token TEXT PRIMARY KEY, entry_key TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    connection.commit()


def _delete_expired(connection: sqlite3.Connection, now_iso: str) -> None:
    connection.execute("DELETE FROM music_cast_tokens WHERE expires_at <= ?", (now_iso,))


def create(settings: Any, entry_key: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict:
    """Mint a fresh token good for ``entry_key`` only. Returns
    ``{"token", "expires_at"}`` -- ``expires_at`` is an ISO-8601 string, for
    callers that want to show/log when the cast link goes stale."""
    token = secrets.token_urlsafe(32)
    expires_at = (_now() + timedelta(seconds=int(ttl_seconds))).replace(microsecond=0).isoformat()
    with _open(settings.userdata_root) as connection:
        _delete_expired(connection, _now().isoformat())  # opportunistic sweep, same pattern as session cleanup
        connection.execute(
            "INSERT INTO music_cast_tokens (token, entry_key, expires_at) VALUES (?, ?, ?)",
            (token, entry_key, expires_at),
        )
        connection.commit()
    return {"token": token, "expires_at": expires_at}


def verify(settings: Any, entry_key: str, token: str) -> bool:
    """True if ``token`` is live, unexpired, and was minted for exactly
    ``entry_key`` -- a token for one track can't be replayed against
    another's stream URL."""
    if not token:
        return False
    with _open(settings.userdata_root) as connection:
        row = connection.execute(
            "SELECT entry_key, expires_at FROM music_cast_tokens WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return False
        if row[1] <= _now().isoformat():
            connection.execute("DELETE FROM music_cast_tokens WHERE token = ?", (token,))
            connection.commit()
            return False
    return row[0] == entry_key


def revoke(settings: Any, token: str) -> None:
    """Invalidate one token, primarily when stream preparation fails."""
    if not token:
        return
    with _open(settings.userdata_root) as connection:
        connection.execute("DELETE FROM music_cast_tokens WHERE token = ?", (token,))
        connection.commit()
