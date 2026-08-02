"""Movie file scanning, fingerprinting, and change tracking for the Drone.

Mirrors ``saves_store.py`` for movie files under ``/userdata/movies``, with one
deliberate simplification: unlike ROMs, BIOS, or saves, movies have **no**
system or artwork association at all -- this is a flat inventory (no
per-system grouping column), and callers (the Transfers UI, peer inventory)
should skip the whole system/artwork picker for this asset type rather than
inventing a fake grouping for it.

Same four responsibilities as the ROM/BIOS/saves inventories:

* scan every movie file on disk and compute a content fingerprint (the same
  sampled ``sample-fp-v1`` hash ROMs/saves use, so identical files share an
  identity across drones),
* persist one row per movie in SQLite, detecting created/updated/deleted files
  by comparing size + modified-time and re-fingerprinting only when those
  change,
* queue every change so the pending-changes view reflects exactly what
  changed since the last clean point, and
* compute a whole-set "thumbprint" so a peer can tell when a re-sync is
  needed (identical contract to the ROM/BIOS/saves thumbprints).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from .state_store import database_path as _state_database_path
    from .state_store import open_database as _open_state_database
    from ..common import fingerprint as _fp
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import open_database as _open_state_database  # type: ignore
    from common import fingerprint as _fp  # type: ignore


MOVIES_FINGERPRINT_ALGORITHM = _fp.FINGERPRINT_ALGORITHM

# Recognized video file extensions -- movies_root can accumulate non-video
# files alongside the real ones (scraper metadata XML, poster/thumbnail
# images, .nfo files, partial ".part"/".lock" files from an in-progress
# download, ...) that must never show up in the movie library or get synced/
# transferred as if they were one. An allowlist (rather than trying to
# enumerate every junk suffix that might show up) is the only thing that
# actually guarantees that.
_VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv",
    ".mpg", ".mpeg", ".m2ts", ".ts", ".3gp",
}


def default_movies_root() -> Path:
    return Path(os.environ.get("MOVIES_ROOT", "/userdata/movies"))


@dataclass(frozen=True)
class MovieEntry:
    entry_key: str
    file_path: str       # relative to movies_root, posix-normalized
    movie_name: str
    absolute_path: str
    file_size: int
    modified_time: int
    fingerprint: str

    def to_payload(self) -> dict:
        return {
            "entry_key": self.entry_key,
            "movie_name": self.movie_name,
            "name": self.movie_name,
            "file_path": self.file_path,
            "relative_path": self.file_path,
            "absolute_path": self.absolute_path,
            "file_size": self.file_size,
            "byte_count": self.file_size,
            "modified_time": self.modified_time,
            "mtime": self.modified_time,
            "fingerprint": self.fingerprint,
            "movies_fingerprint": self.fingerprint,
        }


def build_movie_fingerprint(path: Path) -> str:
    """Sampled content fingerprint (``sample-fp-v1``); identical to ROM/saves fingerprints."""
    return _fp.build_fingerprint(path)


def _normalize_path(value: object) -> str:
    return str(value or "").replace("\\", "/").strip().lstrip("./")


def _entry_key(relative_path: str) -> str:
    raw = relative_path.lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _movies_db_path(movies_root: Path) -> Path:
    # The movies DB lives beside the ROM/saves caches (same drone-app dir) but
    # in its own file so scans never contend on the same tables.
    return _state_database_path(movies_root.parent)


def _open(movies_root: Path) -> sqlite3.Connection:
    connection = _open_state_database(_movies_db_path(movies_root))
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS movies_cache_entries ("
        "entry_key TEXT PRIMARY KEY, file_path TEXT NOT NULL UNIQUE, movie_name TEXT NOT NULL, "
        "absolute_path TEXT, file_size INTEGER NOT NULL DEFAULT 0, modified_time INTEGER NOT NULL DEFAULT 0, "
        "fingerprint TEXT)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS deleted_movies_cache_entries ("
        "entry_key TEXT PRIMARY KEY, file_path TEXT NOT NULL, movie_name TEXT NOT NULL, "
        "absolute_path TEXT, file_size INTEGER NOT NULL DEFAULT 0, modified_time INTEGER NOT NULL DEFAULT 0, "
        "fingerprint TEXT)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS movies_cache_changes ("
        "entry_key TEXT PRIMARY KEY, operation TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_movies_cache_page "
        "ON movies_cache_entries(file_path COLLATE NOCASE, entry_key)"
    )
    connection.commit()


def _iter_movie_files(movies_root: Path):
    if not movies_root.exists() or not movies_root.is_dir():
        return
    root = movies_root.resolve()
    for current_root, _dirs, file_names in os.walk(root):
        for name in sorted(file_names):
            file_path = Path(current_root) / name
            if file_path.suffix.lower() not in _VIDEO_SUFFIXES:
                continue
            try:
                if not file_path.is_file() or file_path.is_symlink():
                    continue
            except OSError:
                continue
            yield file_path, root


def scan_movies(movies_root: Path) -> list[MovieEntry]:
    """Scan ``movies_root`` and return one MovieEntry per movie file."""
    entries: list[MovieEntry] = []
    for file_path, root in _iter_movie_files(movies_root):
        try:
            stat = file_path.stat()
        except OSError:
            continue
        relative = file_path.resolve().relative_to(root).as_posix()
        try:
            fingerprint = build_movie_fingerprint(file_path)
        except OSError:
            continue
        entries.append(
            MovieEntry(
                entry_key=_entry_key(relative),
                file_path=relative,
                movie_name=Path(relative).name,
                absolute_path=str(file_path.resolve()),
                file_size=int(stat.st_size),
                modified_time=int(stat.st_mtime),
                fingerprint=fingerprint,
            )
        )
    return entries


def _read_existing(connection: sqlite3.Connection) -> dict[str, tuple[int, int, str]]:
    rows = connection.execute(
        "SELECT entry_key, file_size, modified_time, fingerprint FROM movies_cache_entries"
    ).fetchall()
    return {row[0]: (int(row[1] or 0), int(row[2] or 0), row[3] or "") for row in rows}


def sync_movies_cache(movies_root: Path) -> dict:
    """Scan disk, reconcile against the cache, and queue created/updated/deleted changes.

    Returns a summary ``{"created", "updated", "deleted", "total", "thumbprint"}``.
    """
    scanned = scan_movies(movies_root)
    scanned_by_key = {entry.entry_key: entry for entry in scanned}
    created = updated = deleted = 0
    with _open(movies_root) as connection:
        existing = _read_existing(connection)
        for key, entry in scanned_by_key.items():
            prior = existing.get(key)
            if prior is None:
                created += 1
            elif prior == (entry.file_size, entry.modified_time, entry.fingerprint):
                continue  # unchanged
            else:
                updated += 1
            _upsert(connection, entry)
            _queue_change(connection, key, "upsert")
        for key in existing.keys() - scanned_by_key.keys():
            _archive_deleted(connection, key)
            connection.execute("DELETE FROM movies_cache_entries WHERE entry_key = ?", (key,))
            _queue_change(connection, key, "delete")
            deleted += 1
        connection.commit()
    return {
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "total": len(scanned),
        "thumbprint": movies_inventory_thumbprint(scanned),
    }


def _upsert(connection: sqlite3.Connection, entry: MovieEntry) -> None:
    connection.execute(
        "INSERT INTO movies_cache_entries (entry_key, file_path, movie_name, absolute_path, file_size, modified_time, fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(entry_key) DO UPDATE SET file_path=excluded.file_path, movie_name=excluded.movie_name, "
        "absolute_path=excluded.absolute_path, file_size=excluded.file_size, modified_time=excluded.modified_time, fingerprint=excluded.fingerprint",
        (
            entry.entry_key,
            entry.file_path,
            entry.movie_name,
            entry.absolute_path,
            entry.file_size,
            entry.modified_time,
            entry.fingerprint,
        ),
    )
    connection.execute("DELETE FROM deleted_movies_cache_entries WHERE entry_key = ?", (entry.entry_key,))


def _archive_deleted(connection: sqlite3.Connection, entry_key: str) -> None:
    row = connection.execute(
        "SELECT entry_key, file_path, movie_name, absolute_path, file_size, modified_time, fingerprint "
        "FROM movies_cache_entries WHERE entry_key = ?",
        (entry_key,),
    ).fetchone()
    if not row:
        return
    connection.execute(
        "INSERT INTO deleted_movies_cache_entries (entry_key, file_path, movie_name, absolute_path, file_size, modified_time, fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(entry_key) DO UPDATE SET "
        "file_path=excluded.file_path, movie_name=excluded.movie_name, absolute_path=excluded.absolute_path, "
        "file_size=excluded.file_size, modified_time=excluded.modified_time, fingerprint=excluded.fingerprint",
        row,
    )


def _queue_change(connection: sqlite3.Connection, entry_key: str, operation: str) -> None:
    connection.execute(
        "INSERT INTO movies_cache_changes (entry_key, operation) VALUES (?, ?) "
        "ON CONFLICT(entry_key) DO UPDATE SET operation=excluded.operation",
        (entry_key, operation),
    )


def list_movies(movies_root: Path) -> list[dict]:
    """Return cached movie rows as upload-ready payloads."""
    with _open(movies_root) as connection:
        rows = connection.execute(
            "SELECT file_path, movie_name, absolute_path, file_size, modified_time, fingerprint "
            "FROM movies_cache_entries ORDER BY file_path"
        ).fetchall()
    return [
        MovieEntry(
            entry_key=_entry_key(row[0]),
            file_path=row[0],
            movie_name=row[1],
            absolute_path=row[2] or "",
            file_size=int(row[3] or 0),
            modified_time=int(row[4] or 0),
            fingerprint=row[5] or "",
        ).to_payload()
        for row in rows
    ]


def get_movie_by_key(movies_root: Path, entry_key: str) -> Optional[dict]:
    """Return one cached movie row by its ``entry_key`` (the stable,
    path-based id used to identify a movie for streaming/download), or
    ``None`` if unknown -- callers should treat that as a 404."""
    with _open(movies_root) as connection:
        row = connection.execute(
            "SELECT file_path, movie_name, absolute_path, file_size, modified_time, fingerprint "
            "FROM movies_cache_entries WHERE entry_key = ?",
            (entry_key,),
        ).fetchone()
    if not row:
        return None
    return MovieEntry(
        entry_key=entry_key,
        file_path=row[0],
        movie_name=row[1],
        absolute_path=row[2] or "",
        file_size=int(row[3] or 0),
        modified_time=int(row[4] or 0),
        fingerprint=row[5] or "",
    ).to_payload()


def list_movies_page(
    movies_root: Path,
    *,
    query: str = "",
    limit: int = 500,
    offset: int = 0,
) -> dict:
    """Return a filtered movie page and total directly from SQLite."""
    safe_limit = max(1, min(int(limit), 2000))
    safe_offset = max(0, int(offset))
    normalized_query = str(query or "").strip()
    where_parts: list[str] = []
    parameters: list = []
    if normalized_query:
        escaped = normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where_parts.append(
            "(movie_name COLLATE NOCASE LIKE ? ESCAPE '\\' "
            "OR file_path COLLATE NOCASE LIKE ? ESCAPE '\\' "
            "OR fingerprint COLLATE NOCASE LIKE ? ESCAPE '\\')"
        )
        parameters.extend([pattern, pattern, pattern])
    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
    columns = "file_path, movie_name, absolute_path, file_size, modified_time, fingerprint"
    with _open(movies_root) as connection:
        total = int(connection.execute(f"SELECT COUNT(*) FROM movies_cache_entries{where}", parameters).fetchone()[0])
        rows = connection.execute(
            f"SELECT {columns} FROM movies_cache_entries{where} "
            "ORDER BY file_path COLLATE NOCASE, entry_key LIMIT ? OFFSET ?",
            [*parameters, safe_limit, safe_offset],
        ).fetchall()
    items = [
        MovieEntry(
            entry_key=_entry_key(row[0]),
            file_path=row[0],
            movie_name=row[1],
            absolute_path=row[2] or "",
            file_size=int(row[3] or 0),
            modified_time=int(row[4] or 0),
            fingerprint=row[5] or "",
        ).to_payload()
        for row in rows
    ]
    return {"total": total, "limit": safe_limit, "offset": safe_offset, "items": items}


def read_pending_changes(movies_root: Path) -> dict:
    """Return queued movies changes as ``{"movies": [...], "deleted": [...]}`` payloads."""
    changes: dict = {"movies": [], "deleted": []}
    with _open(movies_root) as connection:
        for entry_key, operation in connection.execute(
            "SELECT entry_key, operation FROM movies_cache_changes ORDER BY entry_key"
        ).fetchall():
            if operation == "delete":
                row = connection.execute(
                    "SELECT file_path, movie_name, absolute_path, file_size, modified_time, fingerprint "
                    "FROM deleted_movies_cache_entries WHERE entry_key = ?",
                    (entry_key,),
                ).fetchone()
                bucket = "deleted"
            else:
                row = connection.execute(
                    "SELECT file_path, movie_name, absolute_path, file_size, modified_time, fingerprint "
                    "FROM movies_cache_entries WHERE entry_key = ?",
                    (entry_key,),
                ).fetchone()
                bucket = "movies"
            if not row:
                continue
            changes[bucket].append(
                MovieEntry(
                    entry_key=entry_key,
                    file_path=row[0],
                    movie_name=row[1],
                    absolute_path=row[2] or "",
                    file_size=int(row[3] or 0),
                    modified_time=int(row[4] or 0),
                    fingerprint=row[5] or "",
                ).to_payload()
            )
    return changes


def clear_pending_changes(movies_root: Path) -> None:
    with _open(movies_root) as connection:
        connection.execute("DELETE FROM movies_cache_changes")
        connection.execute("DELETE FROM deleted_movies_cache_entries")
        connection.commit()


def movies_inventory_thumbprint(entries) -> str:
    """SHA256 over the sorted movie set (path, fingerprint, size).

    Accepts either MovieEntry objects or upload payload dicts so callers can
    thumbprint a fresh scan or the cached rows interchangeably.
    """
    rows = []
    for entry in entries or []:
        if isinstance(entry, MovieEntry):
            path, fingerprint, size = entry.file_path, entry.fingerprint, entry.file_size
        elif isinstance(entry, dict):
            path = _normalize_path(entry.get("file_path") or entry.get("relative_path"))
            fingerprint = str(entry.get("fingerprint") or entry.get("movies_fingerprint") or "")
            size = entry.get("file_size") or entry.get("byte_count") or 0
        else:
            continue
        path = _normalize_path(path).lower()
        if not path:
            continue
        size_value = str(int(size)) if isinstance(size, (int, float)) else str(size or "").strip()
        rows.append("\t".join((path, fingerprint.strip().lower(), size_value)))
    digest = hashlib.sha256()
    for value in sorted(rows):
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def stored_thumbprint(movies_root: Path) -> str:
    """Compute the thumbprint from the current cache rows (no disk re-scan)."""
    return movies_inventory_thumbprint(list_movies(movies_root))
