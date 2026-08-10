"""Music file scanning, fingerprinting, and change tracking for the Drone.

Mirrors ``movies_store.py`` for music files under ``/userdata/music``, with the
same deliberate simplification movies uses: this is a flat inventory (no
per-system grouping column) -- artist/album grouping is computed from each
file's folder path at read time (see ``music/filename_parser.py``), never
stored as a relational parent/child table, for the identical reason movies'
show/season grouping isn't one: a partially-scraped album must not visually
split into two cards just because its grouping key lives in two places that
can drift apart.

Same four responsibilities as the ROM/BIOS/saves/movies inventories:

* scan every music file on disk and compute a content fingerprint (the same
  sampled ``sample-fp-v1`` hash ROMs/saves/movies use, so identical files
  share an identity across drones),
* persist one row per track in SQLite, detecting created/updated/deleted files
  by comparing size + modified-time and re-fingerprinting only when those
  change,
* queue every change so the pending-changes view reflects exactly what
  changed since the last clean point, and
* compute a whole-set "thumbprint" so a peer can tell when a re-sync is
  needed (identical contract to the ROM/BIOS/saves/movies thumbprints).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
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


MUSIC_FINGERPRINT_ALGORITHM = _fp.FINGERPRINT_ALGORITHM

# Recognized audio file extensions -- music_root can accumulate non-audio
# files alongside the real ones (scraper cover art, .cue/.log sidecar files,
# playlists) that must never show up in the music library or get synced/
# transferred as if they were a track. An allowlist (rather than trying to
# enumerate every junk suffix that might show up) is the only thing that
# actually guarantees that -- same reasoning as movies' _VIDEO_SUFFIXES.
_AUDIO_SUFFIXES = {
    ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".wma", ".aac", ".aiff", ".ape",
}

# Conventionally-named local cover art -- the same "folder.jpg"/"cover.jpg"
# convention Plex/Kodi/most media servers already recognize. Used as a
# fallback when a track has no *scraped* art (never scraped, or scraped but
# Cover Art Archive had nothing for that release) -- see
# find_local_cover_image. Deliberately an allowlist of exact basenames, not
# "the first image file in the folder": a folder can contain unrelated
# images (a scan of liner notes, a random screenshot) that would be a wrong
# guess to show as cover art.
_COVER_IMAGE_BASENAMES = ("cover", "folder", "album", "front", "art")
_COVER_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def default_music_root() -> Path:
    return Path(os.environ.get("MUSIC_ROOT", "/userdata/music"))


@dataclass(frozen=True)
class MusicEntry:
    entry_key: str
    file_path: str       # relative to music_root, posix-normalized
    track_name: str
    absolute_path: str
    file_size: int
    modified_time: int
    fingerprint: str

    def to_payload(self) -> dict:
        return {
            "entry_key": self.entry_key,
            "track_name": self.track_name,
            "name": self.track_name,
            "file_path": self.file_path,
            "relative_path": self.file_path,
            "absolute_path": self.absolute_path,
            "file_size": self.file_size,
            "byte_count": self.file_size,
            "modified_time": self.modified_time,
            "mtime": self.modified_time,
            "fingerprint": self.fingerprint,
            "music_fingerprint": self.fingerprint,
        }


def build_music_fingerprint(path: Path) -> str:
    """Sampled content fingerprint (``sample-fp-v1``); identical to ROM/saves/movies fingerprints."""
    return _fp.build_fingerprint(path)


def _normalize_path(value: object) -> str:
    return str(value or "").replace("\\", "/").strip().lstrip("./")


def _entry_key(relative_path: str) -> str:
    raw = relative_path.lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _music_db_path(music_root: Path) -> Path:
    # The music DB lives beside the ROM/saves/movies caches (same drone-app
    # dir) but in its own tables so scans never contend on the same rows.
    return _state_database_path(music_root.parent)


def _open(music_root: Path) -> sqlite3.Connection:
    connection = _open_state_database(_music_db_path(music_root))
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS music_cache_entries ("
        "entry_key TEXT PRIMARY KEY, file_path TEXT NOT NULL UNIQUE, track_name TEXT NOT NULL, "
        "absolute_path TEXT, file_size INTEGER NOT NULL DEFAULT 0, modified_time INTEGER NOT NULL DEFAULT 0, "
        "fingerprint TEXT)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS deleted_music_cache_entries ("
        "entry_key TEXT PRIMARY KEY, file_path TEXT NOT NULL, track_name TEXT NOT NULL, "
        "absolute_path TEXT, file_size INTEGER NOT NULL DEFAULT 0, modified_time INTEGER NOT NULL DEFAULT 0, "
        "fingerprint TEXT)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS music_cache_changes ("
        "entry_key TEXT PRIMARY KEY, operation TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_music_cache_page "
        "ON music_cache_entries(file_path COLLATE NOCASE, entry_key)"
    )
    # Scraped MusicBrainz/Cover Art Archive metadata, one row per track that
    # has been scraped (not every music_cache_entries row has one). title/
    # art_relative_path/artist_art_relative_path are their own columns since
    # they're looked up on every list/detail response; the rest (artist,
    # album, genres, release_date, track_number, disc_number, duration_ms,
    # MusicBrainz ids) is far more likely to grow/change shape over time, so
    # it lives in extra_json -- same "don't keep ALTERing for every optional
    # field" convention movies_metadata_entries/rom_cache_entries/
    # bios_cache_entries/artwork_cache_entries already use.
    connection.execute(
        "CREATE TABLE IF NOT EXISTS music_metadata_entries ("
        "entry_key TEXT PRIMARY KEY, provider TEXT NOT NULL, provider_id TEXT NOT NULL, "
        "title TEXT NOT NULL DEFAULT '', art_relative_path TEXT, artist_art_relative_path TEXT, "
        "scraped_at TEXT NOT NULL, extra_json TEXT NOT NULL DEFAULT '{}')"
    )
    # A liked track's row simply exists -- no "liked BOOLEAN" column with a
    # value to keep in sync, just presence/absence, same shape as the
    # cache_changes queue above. Independent of music_metadata_entries: a
    # track can be liked whether or not it's ever been scraped.
    connection.execute(
        "CREATE TABLE IF NOT EXISTS music_likes (entry_key TEXT PRIMARY KEY, liked_at TEXT NOT NULL)"
    )
    connection.commit()


def _iter_music_files(music_root: Path):
    if not music_root.exists() or not music_root.is_dir():
        return
    root = music_root.resolve()
    for current_root, _dirs, file_names in os.walk(root):
        for name in sorted(file_names):
            file_path = Path(current_root) / name
            if file_path.suffix.lower() not in _AUDIO_SUFFIXES:
                continue
            try:
                if not file_path.is_file() or file_path.is_symlink():
                    continue
            except OSError:
                continue
            yield file_path, root


def scan_music(music_root: Path) -> list[MusicEntry]:
    """Scan ``music_root`` and return one MusicEntry per track file."""
    entries: list[MusicEntry] = []
    for file_path, root in _iter_music_files(music_root):
        try:
            stat = file_path.stat()
        except OSError:
            continue
        relative = file_path.resolve().relative_to(root).as_posix()
        try:
            fingerprint = build_music_fingerprint(file_path)
        except OSError:
            continue
        entries.append(
            MusicEntry(
                entry_key=_entry_key(relative),
                file_path=relative,
                track_name=Path(relative).name,
                absolute_path=str(file_path.resolve()),
                file_size=int(stat.st_size),
                modified_time=int(stat.st_mtime),
                fingerprint=fingerprint,
            )
        )
    return entries


def _read_existing(connection: sqlite3.Connection) -> dict[str, tuple[int, int, str]]:
    rows = connection.execute(
        "SELECT entry_key, file_size, modified_time, fingerprint FROM music_cache_entries"
    ).fetchall()
    return {row[0]: (int(row[1] or 0), int(row[2] or 0), row[3] or "") for row in rows}


def sync_music_cache(music_root: Path) -> dict:
    """Scan disk, reconcile against the cache, and queue created/updated/deleted changes.

    Returns a summary ``{"created", "updated", "deleted", "total", "thumbprint"}``.
    """
    scanned = scan_music(music_root)
    scanned_by_key = {entry.entry_key: entry for entry in scanned}
    created = updated = deleted = 0
    with _open(music_root) as connection:
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
            connection.execute("DELETE FROM music_cache_entries WHERE entry_key = ?", (key,))
            _queue_change(connection, key, "delete")
            deleted += 1
        connection.commit()
    return {
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "total": len(scanned),
        "thumbprint": music_inventory_thumbprint(scanned),
    }


def _upsert(connection: sqlite3.Connection, entry: MusicEntry) -> None:
    connection.execute(
        "INSERT INTO music_cache_entries (entry_key, file_path, track_name, absolute_path, file_size, modified_time, fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(entry_key) DO UPDATE SET file_path=excluded.file_path, track_name=excluded.track_name, "
        "absolute_path=excluded.absolute_path, file_size=excluded.file_size, modified_time=excluded.modified_time, fingerprint=excluded.fingerprint",
        (
            entry.entry_key,
            entry.file_path,
            entry.track_name,
            entry.absolute_path,
            entry.file_size,
            entry.modified_time,
            entry.fingerprint,
        ),
    )
    connection.execute("DELETE FROM deleted_music_cache_entries WHERE entry_key = ?", (entry.entry_key,))


def _archive_deleted(connection: sqlite3.Connection, entry_key: str) -> None:
    row = connection.execute(
        "SELECT entry_key, file_path, track_name, absolute_path, file_size, modified_time, fingerprint "
        "FROM music_cache_entries WHERE entry_key = ?",
        (entry_key,),
    ).fetchone()
    if not row:
        return
    connection.execute(
        "INSERT INTO deleted_music_cache_entries (entry_key, file_path, track_name, absolute_path, file_size, modified_time, fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(entry_key) DO UPDATE SET "
        "file_path=excluded.file_path, track_name=excluded.track_name, absolute_path=excluded.absolute_path, "
        "file_size=excluded.file_size, modified_time=excluded.modified_time, fingerprint=excluded.fingerprint",
        row,
    )


def _queue_change(connection: sqlite3.Connection, entry_key: str, operation: str) -> None:
    connection.execute(
        "INSERT INTO music_cache_changes (entry_key, operation) VALUES (?, ?) "
        "ON CONFLICT(entry_key) DO UPDATE SET operation=excluded.operation",
        (entry_key, operation),
    )


def list_music(music_root: Path) -> list[dict]:
    """Return cached music rows as upload-ready payloads."""
    with _open(music_root) as connection:
        rows = connection.execute(
            "SELECT file_path, track_name, absolute_path, file_size, modified_time, fingerprint "
            "FROM music_cache_entries ORDER BY file_path"
        ).fetchall()
    return [
        MusicEntry(
            entry_key=_entry_key(row[0]),
            file_path=row[0],
            track_name=row[1],
            absolute_path=row[2] or "",
            file_size=int(row[3] or 0),
            modified_time=int(row[4] or 0),
            fingerprint=row[5] or "",
        ).to_payload()
        for row in rows
    ]


def list_music_under_artist_folder(music_root: Path, artist_folder: str) -> list[dict]:
    """Return ``{entry_key, file_path}`` pairs for every track whose
    ``file_path`` starts with ``<artist_folder>/`` -- a cheap,
    prefix-scoped alternative to scanning the whole library. Used to find
    sibling tracks within the same on-disk artist folder (e.g. album-level
    artwork fallback: if this track has no scraped art of its own, another
    track from the same album might) without a full-table
    classify_location pass over every track in the library."""
    artist_folder = str(artist_folder or "").strip()
    if not artist_folder:
        return []
    escaped = artist_folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"{escaped}/%"
    with _open(music_root) as connection:
        rows = connection.execute(
            "SELECT entry_key, file_path FROM music_cache_entries WHERE file_path LIKE ? ESCAPE '\\'",
            (pattern,),
        ).fetchall()
    return [{"entry_key": row[0], "file_path": row[1]} for row in rows]


def get_music_by_key(music_root: Path, entry_key: str) -> Optional[dict]:
    """Return one cached music row by its ``entry_key`` (the stable,
    path-based id used to identify a track for streaming/download), or
    ``None`` if unknown -- callers should treat that as a 404."""
    with _open(music_root) as connection:
        row = connection.execute(
            "SELECT file_path, track_name, absolute_path, file_size, modified_time, fingerprint "
            "FROM music_cache_entries WHERE entry_key = ?",
            (entry_key,),
        ).fetchone()
    if not row:
        return None
    return MusicEntry(
        entry_key=entry_key,
        file_path=row[0],
        track_name=row[1],
        absolute_path=row[2] or "",
        file_size=int(row[3] or 0),
        modified_time=int(row[4] or 0),
        fingerprint=row[5] or "",
    ).to_payload()


# entry_key is always a hex-digest slice _entry_key() produces -- rejecting
# anything else before it ever touches the filesystem, same convention as
# movies_store._ENTRY_KEY_RE.
_ENTRY_KEY_RE = re.compile(r"^[0-9a-f]{1,64}$")


def resolve_music_stream_path(music_root: Path, entry_key: str) -> Path:
    """Look up a track by its stable id and validate the resolved path stays
    inside ``music_root`` -- the one path-traversal-safe lookup every
    music-file-serving handler shares, mirroring
    ``movies_store.resolve_movie_stream_path``."""
    if not _ENTRY_KEY_RE.match(str(entry_key or "")):
        raise FileNotFoundError()
    row = get_music_by_key(music_root, entry_key)
    if not row:
        raise FileNotFoundError()
    resolved_root = Path(music_root).resolve()
    target = (resolved_root / row["file_path"]).resolve()
    if target == resolved_root or resolved_root not in target.parents or not target.is_file():
        raise FileNotFoundError()
    return target


def find_local_cover_image(track_absolute_path: Path, music_root: Path) -> Optional[Path]:
    """Best-effort local fallback artwork for a track with nothing scraped
    (or scraped with no art found) -- a conventionally-named image file
    (``_COVER_IMAGE_BASENAMES``) directly beside the track, then one level up
    (the artist root, for a library that keeps one cover image at
    ``Artist/cover.jpg`` rather than per-album). Matched case-insensitively
    (real libraries mix ``Cover.jpg``/``folder.JPG``/...). Returns the first
    match, preferring the track's own folder over its parent; ``None`` if
    nothing recognized is present in either."""
    resolved_root = music_root.resolve()
    track_dir = track_absolute_path.parent
    search_dirs = [track_dir]
    parent_dir = track_dir.parent
    if track_dir.resolve() != resolved_root and parent_dir.resolve() != resolved_root:
        search_dirs.append(parent_dir)
    for directory in search_dirs:
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        by_lower_stem = {
            entry.stem.lower(): entry
            for entry in entries
            if entry.is_file() and entry.suffix.lower() in _COVER_IMAGE_SUFFIXES
        }
        for basename in _COVER_IMAGE_BASENAMES:
            match = by_lower_stem.get(basename)
            if match:
                return match
    return None


def get_music_metadata(music_root: Path, entry_key: str) -> Optional[dict]:
    """Return one track's scraped MusicBrainz metadata, or ``None`` if it has
    never been scraped -- distinct from ``get_music_by_key`` returning
    ``None`` for an unknown *file*; a track can exist with no metadata row
    at all."""
    with _open(music_root) as connection:
        row = connection.execute(
            "SELECT provider, provider_id, title, art_relative_path, artist_art_relative_path, scraped_at, extra_json "
            "FROM music_metadata_entries WHERE entry_key = ?",
            (entry_key,),
        ).fetchone()
    if not row:
        return None
    try:
        extra = json.loads(row[6] or "{}")
    except (TypeError, ValueError):
        extra = {}
    return {
        "entry_key": entry_key,
        "provider": row[0],
        "provider_id": row[1],
        "title": row[2] or "",
        "art_relative_path": row[3],
        "artist_art_relative_path": row[4],
        "scraped_at": row[5],
        **extra,
    }


def save_music_metadata(
    music_root: Path,
    entry_key: str,
    *,
    provider: str,
    provider_id: str,
    title: str,
    art_relative_path: Optional[str],
    artist_art_relative_path: Optional[str],
    extra: dict,
) -> dict:
    """Upsert one track's scraped metadata. ``extra`` is any JSON-serializable
    dict (artist, album, genres, release_date, track_number, disc_number,
    duration_ms, artist_mbid, release_mbid, release_group_mbid,
    recording_mbid) -- see the module docstring's note on why this is a
    single JSON blob column rather than one column per field."""
    scraped_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    extra_json = json.dumps(extra or {})
    with _open(music_root) as connection:
        connection.execute(
            "INSERT INTO music_metadata_entries "
            "(entry_key, provider, provider_id, title, art_relative_path, artist_art_relative_path, scraped_at, extra_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entry_key) DO UPDATE SET provider=excluded.provider, provider_id=excluded.provider_id, "
            "title=excluded.title, art_relative_path=excluded.art_relative_path, "
            "artist_art_relative_path=excluded.artist_art_relative_path, scraped_at=excluded.scraped_at, "
            "extra_json=excluded.extra_json",
            (entry_key, provider, str(provider_id), title or "", art_relative_path, artist_art_relative_path, scraped_at, extra_json),
        )
        connection.commit()
    return get_music_metadata(music_root, entry_key)


def set_music_art(music_root: Path, entry_key: str, art_relative_path: str) -> dict:
    """Set (or replace) just a track's ``art_relative_path`` -- used by the
    manual album-cover upload path, which must not clobber a track's real
    scraped title/genres/MusicBrainz ids just because the user wants to
    swap in a different image. Creates a minimal ``provider="manual"`` row
    (mirroring ``save_music_metadata``'s upsert shape) if the track had
    never been scraped/uploaded before; otherwise only the art column
    changes, everything else untouched."""
    with _open(music_root) as connection:
        cursor = connection.execute(
            "UPDATE music_metadata_entries SET art_relative_path = ? WHERE entry_key = ?",
            (art_relative_path, entry_key),
        )
        if cursor.rowcount == 0:
            scraped_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            connection.execute(
                "INSERT INTO music_metadata_entries "
                "(entry_key, provider, provider_id, title, art_relative_path, artist_art_relative_path, scraped_at, extra_json) "
                "VALUES (?, 'manual', '', '', ?, NULL, ?, '{}')",
                (entry_key, art_relative_path, scraped_at),
            )
        connection.commit()
    return get_music_metadata(music_root, entry_key)


def delete_music_metadata(music_root: Path, entry_key: str) -> Optional[dict]:
    """Remove one track's scraped MusicBrainz metadata row -- for when a
    scrape matched the wrong track/album and a human needs to clear it before
    re-scraping. Returns what was deleted (including
    ``art_relative_path``/``artist_art_relative_path``, so a caller can also
    remove the artwork files those columns point at) or ``None`` if the track
    had no metadata row to begin with -- that's a normal, idempotent no-op,
    not an error; a track file existing with nothing ever scraped for it is
    the default state for most of a library."""
    existing = get_music_metadata(music_root, entry_key)
    if not existing:
        return None
    with _open(music_root) as connection:
        connection.execute("DELETE FROM music_metadata_entries WHERE entry_key = ?", (entry_key,))
        connection.commit()
    return existing


def delete_music_file(music_root: Path, entry_key: str) -> dict:
    """Permanently delete one track's file from disk plus its cache entry.
    An unknown entry_key is a no-op (``{"deleted": False}``), not an error,
    same convention as ``delete_music_metadata``. The file is unlinked
    *before* the cache row is removed: if the unlink fails (e.g. a read-only
    mount), the row -- and so the track's visibility in the UI -- is left
    untouched rather than making a file that's still really on disk silently
    disappear from the library until the next full rescan happens to re-add
    it."""
    row = get_music_by_key(music_root, entry_key)
    if not row:
        return {"deleted": False}
    resolved_root = Path(music_root).resolve()
    target = (resolved_root / row["file_path"]).resolve()
    if target == resolved_root or resolved_root not in target.parents:
        return {"deleted": False}
    target.unlink(missing_ok=True)
    with _open(music_root) as connection:
        _archive_deleted(connection, entry_key)
        connection.execute("DELETE FROM music_cache_entries WHERE entry_key = ?", (entry_key,))
        _queue_change(connection, entry_key, "delete")
        connection.commit()
    return {"deleted": True, "file_path": row["file_path"]}


def list_music_display_titles(music_root: Path) -> dict:
    """Return ``{entry_key: scraped_title}`` for every track that has been
    scraped -- used to overlay a clean title onto the plain list response
    without a JOIN in every list query."""
    with _open(music_root) as connection:
        rows = connection.execute("SELECT entry_key, title FROM music_metadata_entries WHERE title != ''").fetchall()
    return {row[0]: row[1] for row in rows}


def list_music_genres(music_root: Path) -> dict:
    """Return ``{entry_key: [genre, ...]}`` for every track that has been
    scraped and has at least one genre -- same bulk-lookup shape as
    ``list_music_display_titles``, used to overlay genres onto the plain
    list response so the Music Explorer's category sidebar doesn't need a
    separate request per track. ``genres`` lives in ``extra_json`` (see the
    module docstring on that column), not its own column."""
    with _open(music_root) as connection:
        rows = connection.execute("SELECT entry_key, extra_json FROM music_metadata_entries").fetchall()
    genres_by_key = {}
    for entry_key, extra_json in rows:
        try:
            extra = json.loads(extra_json or "{}")
        except (TypeError, ValueError):
            continue
        genres = extra.get("genres") if isinstance(extra, dict) else None
        if genres:
            genres_by_key[entry_key] = list(genres)
    return genres_by_key


def set_music_liked(music_root: Path, entry_key: str, liked: bool) -> bool:
    """Set or clear a track's liked flag. Idempotent either way (liking an
    already-liked track, or unliking one that was never liked, is a normal
    no-op, not an error) -- returns the resulting state."""
    with _open(music_root) as connection:
        if liked:
            connection.execute(
                "INSERT INTO music_likes (entry_key, liked_at) VALUES (?, ?) "
                "ON CONFLICT(entry_key) DO NOTHING",
                (entry_key, datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
            )
        else:
            connection.execute("DELETE FROM music_likes WHERE entry_key = ?", (entry_key,))
        connection.commit()
    return liked


def is_music_liked(music_root: Path, entry_key: str) -> bool:
    with _open(music_root) as connection:
        row = connection.execute("SELECT 1 FROM music_likes WHERE entry_key = ?", (entry_key,)).fetchone()
    return row is not None


def list_liked_music_keys(music_root: Path) -> set:
    """Bulk lookup of every liked entry_key -- same one-query-per-list-call
    shape as ``list_music_genres``/``list_music_display_titles``, used to
    overlay a ``liked`` flag onto list responses without a query per row."""
    with _open(music_root) as connection:
        rows = connection.execute("SELECT entry_key FROM music_likes").fetchall()
    return {row[0] for row in rows}


def list_music_page(
    music_root: Path,
    *,
    query: str = "",
    limit: int = 500,
    offset: int = 0,
) -> dict:
    """Return a filtered music page and total directly from SQLite."""
    safe_limit = max(1, min(int(limit), 2000))
    safe_offset = max(0, int(offset))
    normalized_query = str(query or "").strip()
    where_parts: list[str] = []
    parameters: list = []
    if normalized_query:
        escaped = normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where_parts.append(
            "(track_name COLLATE NOCASE LIKE ? ESCAPE '\\' "
            "OR file_path COLLATE NOCASE LIKE ? ESCAPE '\\' "
            "OR fingerprint COLLATE NOCASE LIKE ? ESCAPE '\\')"
        )
        parameters.extend([pattern, pattern, pattern])
    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
    columns = "file_path, track_name, absolute_path, file_size, modified_time, fingerprint"
    with _open(music_root) as connection:
        total = int(connection.execute(f"SELECT COUNT(*) FROM music_cache_entries{where}", parameters).fetchone()[0])
        rows = connection.execute(
            f"SELECT {columns} FROM music_cache_entries{where} "
            "ORDER BY file_path COLLATE NOCASE, entry_key LIMIT ? OFFSET ?",
            [*parameters, safe_limit, safe_offset],
        ).fetchall()
    items = [
        MusicEntry(
            entry_key=_entry_key(row[0]),
            file_path=row[0],
            track_name=row[1],
            absolute_path=row[2] or "",
            file_size=int(row[3] or 0),
            modified_time=int(row[4] or 0),
            fingerprint=row[5] or "",
        ).to_payload()
        for row in rows
    ]
    return {"total": total, "limit": safe_limit, "offset": safe_offset, "items": items}


def read_pending_changes(music_root: Path) -> dict:
    """Return queued music changes as ``{"music": [...], "deleted": [...]}`` payloads."""
    changes: dict = {"music": [], "deleted": []}
    with _open(music_root) as connection:
        for entry_key, operation in connection.execute(
            "SELECT entry_key, operation FROM music_cache_changes ORDER BY entry_key"
        ).fetchall():
            if operation == "delete":
                row = connection.execute(
                    "SELECT file_path, track_name, absolute_path, file_size, modified_time, fingerprint "
                    "FROM deleted_music_cache_entries WHERE entry_key = ?",
                    (entry_key,),
                ).fetchone()
                bucket = "deleted"
            else:
                row = connection.execute(
                    "SELECT file_path, track_name, absolute_path, file_size, modified_time, fingerprint "
                    "FROM music_cache_entries WHERE entry_key = ?",
                    (entry_key,),
                ).fetchone()
                bucket = "music"
            if not row:
                continue
            changes[bucket].append(
                MusicEntry(
                    entry_key=entry_key,
                    file_path=row[0],
                    track_name=row[1],
                    absolute_path=row[2] or "",
                    file_size=int(row[3] or 0),
                    modified_time=int(row[4] or 0),
                    fingerprint=row[5] or "",
                ).to_payload()
            )
    return changes


def clear_pending_changes(music_root: Path) -> None:
    with _open(music_root) as connection:
        connection.execute("DELETE FROM music_cache_changes")
        connection.execute("DELETE FROM deleted_music_cache_entries")
        connection.commit()


def music_inventory_thumbprint(entries) -> str:
    """SHA256 over the sorted music set (path, fingerprint, size).

    Accepts either MusicEntry objects or upload payload dicts so callers can
    thumbprint a fresh scan or the cached rows interchangeably.
    """
    rows = []
    for entry in entries or []:
        if isinstance(entry, MusicEntry):
            path, fingerprint, size = entry.file_path, entry.fingerprint, entry.file_size
        elif isinstance(entry, dict):
            path = _normalize_path(entry.get("file_path") or entry.get("relative_path"))
            fingerprint = str(entry.get("fingerprint") or entry.get("music_fingerprint") or "")
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


def stored_thumbprint(music_root: Path) -> str:
    """Compute the thumbprint from the current cache rows (no disk re-scan)."""
    return music_inventory_thumbprint(list_music(music_root))
