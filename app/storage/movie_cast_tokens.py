"""Short-lived, single-movie-scoped tokens that authorize streaming one
movie without the browser's session cookie.

Exists for casting (Chromecast/AirPlay): the receiver device fetches the
video URL itself, directly, with no browser in the loop -- it can't carry
the session cookie the rest of this app's browsing surface requires, and
even if it could, this Drone's self-signed HTTPS cert would block it
anyway (see ``drone_api.py``'s ``_CastHttpHandler``, the plain-HTTP
listener these tokens gate). A token is minted by an already-authenticated
request (``handlers_movies.py``'s ``_handle_movie_cast_token_create``) and
is good for exactly one movie -- knowing the token for "Movie A" proves
nothing about "Movie B".

Same SQLite-row-is-the-source-of-truth shape as ``storage/state_store.py``'s
session tokens (``opaque random string, looked up by value``), not a
signed/stateless token -- consistent with how the rest of this app already
does auth, and it means a token can be individually invalidated (not
needed today, but keeps the door open) without a signing-key rotation story.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

try:
    from .state_store import database_path as _state_database_path
    from .state_store import open_database as _open_state_database
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import open_database as _open_state_database  # type: ignore


# A cast session can legitimately run for hours (a long movie, paused and
# resumed) -- generous on purpose, this only ever authorizes reading back
# one specific already-on-disk file, not a capability worth being stingy
# with the clock over.
DEFAULT_TTL_SECONDS = 12 * 60 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _open(userdata_root) -> sqlite3.Connection:
    connection = _open_state_database(_state_database_path(userdata_root))
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS movie_cast_tokens ("
        "token TEXT PRIMARY KEY, entry_key TEXT NOT NULL, expires_at TEXT NOT NULL)"
    )
    connection.commit()


def _delete_expired(connection: sqlite3.Connection, now_iso: str) -> None:
    connection.execute("DELETE FROM movie_cast_tokens WHERE expires_at <= ?", (now_iso,))


def create(settings: Any, entry_key: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict:
    """Mint a fresh token good for ``entry_key`` only. Returns
    ``{"token", "expires_at"}`` -- ``expires_at`` is an ISO-8601 string, for
    callers that want to show/log when the cast link goes stale."""
    token = secrets.token_urlsafe(32)
    expires_at = (_now() + timedelta(seconds=int(ttl_seconds))).replace(microsecond=0).isoformat()
    with _open(settings.userdata_root) as connection:
        _delete_expired(connection, _now().isoformat())  # opportunistic sweep, same pattern as session cleanup
        connection.execute(
            "INSERT INTO movie_cast_tokens (token, entry_key, expires_at) VALUES (?, ?, ?)",
            (token, entry_key, expires_at),
        )
        connection.commit()
    return {"token": token, "expires_at": expires_at}


def verify(settings: Any, entry_key: str, token: str) -> bool:
    """True if ``token`` is live, unexpired, and was minted for exactly
    ``entry_key`` -- a token for one movie can't be replayed against
    another's stream URL."""
    if not token:
        return False
    with _open(settings.userdata_root) as connection:
        row = connection.execute(
            "SELECT entry_key, expires_at FROM movie_cast_tokens WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return False
        if row[1] <= _now().isoformat():
            connection.execute("DELETE FROM movie_cast_tokens WHERE token = ?", (token,))
            connection.commit()
            return False
    return row[0] == entry_key
