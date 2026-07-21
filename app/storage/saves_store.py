"""Game-save scanning, fingerprinting, and change tracking for the Drone.

This mirrors the ROM/BIOS inventory workflow (see ``rom_metadata_store``) for game
saves under ``/userdata/saves``:

* scan every save file on disk and compute a content fingerprint (the same sampled
  ``sample-fp-v1`` hash ROMs use, so identical files share an identity across drones),
* persist one row per save in SQLite, detecting created/updated/deleted files by
  comparing size + modified-time and re-fingerprinting only when those change,
* queue every change so the pending-changes view reflects exactly what changed since
  the last clean point, and
* compute a whole-set "thumbprint" so a peer can tell when a re-sync is needed
  (identical contract to the ROM/BIOS thumbprints).

Save conflicts resolve newest-modified-time-wins, so ``modified_time`` is part of every
row and is what peers compare when deciding which copy propagates.

The module is deliberately self-contained (its own table in the shared state DB) so it
can be unit-tested and wired into the Drone poller without disturbing the ROM cache.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

try:
    from .state_store import database_path as _state_database_path
    from .state_store import open_database as _open_state_database
    from ..common import fingerprint as _fp
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import open_database as _open_state_database  # type: ignore
    from common import fingerprint as _fp  # type: ignore


SAVES_FINGERPRINT_ALGORITHM = _fp.FINGERPRINT_ALGORITHM
SAVES_INVENTORY_FINGERPRINT_ALGORITHM = "saves-inventory-sha256-v1"

# Files Batocera writes next to saves that are not themselves transferable saves.
_IGNORED_SUFFIXES = {".tmp", ".bak", ".lock"}


def default_saves_root() -> Path:
    return Path(os.environ.get("SAVES_ROOT", "/userdata/saves"))


@dataclass(frozen=True)
class SaveEntry:
    entry_key: str
    system: str
    file_path: str       # relative to saves_root, posix-normalized
    save_name: str
    absolute_path: str
    file_size: int
    modified_time: int
    fingerprint: str

    def to_payload(self) -> dict:
        return {
            "system": self.system,
            "system_name": self.system,
            "save_name": self.save_name,
            "name": self.save_name,
            "file_path": self.file_path,
            "relative_path": self.file_path,
            "absolute_path": self.absolute_path,
            "file_size": self.file_size,
            "byte_count": self.file_size,
            "modified_time": self.modified_time,
            "mtime": self.modified_time,
            "fingerprint": self.fingerprint,
            "saves_fingerprint": self.fingerprint,
        }


def build_save_fingerprint(path: Path) -> str:
    """Sampled content fingerprint (``sample-fp-v1``); identical to ROM fingerprints.

    Delegates to the shared ``fingerprint`` module so saves and ROMs share one
    algorithm and the same bytes yield the same identity across drones.
    """
    return _fp.build_fingerprint(path)


def _normalize_path(value: object) -> str:
    return str(value or "").replace("\\", "/").strip().lstrip("./")


def _entry_key(system: str, relative_path: str) -> str:
    raw = f"{system.lower()}|{relative_path.lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _saves_db_path(saves_root: Path) -> Path:
    # The saves DB lives beside the ROM cache (same drone-app dir) but in its own file
    # so the two scans never contend on the same tables.
    return _state_database_path(saves_root.parent)


def _open(saves_root: Path) -> sqlite3.Connection:
    connection = _open_state_database(_saves_db_path(saves_root))
    _ensure_schema(connection)
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS saves_cache_entries ("
        "entry_key TEXT PRIMARY KEY, system TEXT NOT NULL, file_path TEXT NOT NULL, save_name TEXT NOT NULL, "
        "absolute_path TEXT, file_size INTEGER NOT NULL DEFAULT 0, modified_time INTEGER NOT NULL DEFAULT 0, "
        "fingerprint TEXT, UNIQUE(system, file_path))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS deleted_saves_cache_entries ("
        "entry_key TEXT PRIMARY KEY, system TEXT NOT NULL, file_path TEXT NOT NULL, save_name TEXT NOT NULL, "
        "absolute_path TEXT, file_size INTEGER NOT NULL DEFAULT 0, modified_time INTEGER NOT NULL DEFAULT 0, "
        "fingerprint TEXT)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS saves_cache_changes ("
        "entry_key TEXT PRIMARY KEY, operation TEXT NOT NULL)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_saves_cache_system ON saves_cache_entries(system)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_saves_cache_page "
        "ON saves_cache_entries(system COLLATE NOCASE, file_path COLLATE NOCASE, entry_key)"
    )
    connection.commit()


def _iter_save_files(saves_root: Path):
    if not saves_root.exists() or not saves_root.is_dir():
        return
    root = saves_root.resolve()
    for current_root, _dirs, file_names in os.walk(root):
        for name in sorted(file_names):
            file_path = (Path(current_root) / name)
            if file_path.suffix.lower() in _IGNORED_SUFFIXES:
                continue
            try:
                if not file_path.is_file() or file_path.is_symlink():
                    continue
            except OSError:
                continue
            yield file_path, root


def scan_saves(saves_root: Path) -> list[SaveEntry]:
    """Scan ``saves_root`` and return one SaveEntry per save file (system = top dir)."""
    entries: list[SaveEntry] = []
    for file_path, root in _iter_save_files(saves_root):
        try:
            stat = file_path.stat()
        except OSError:
            continue
        relative = file_path.resolve().relative_to(root).as_posix()
        system = relative.split("/", 1)[0] if "/" in relative else ""
        try:
            fingerprint = build_save_fingerprint(file_path)
        except OSError:
            continue
        entries.append(
            SaveEntry(
                entry_key=_entry_key(system, relative),
                system=system,
                file_path=relative,
                save_name=Path(relative).name,
                absolute_path=str(file_path.resolve()),
                file_size=int(stat.st_size),
                modified_time=int(stat.st_mtime),
                fingerprint=fingerprint,
            )
        )
    return entries


def _read_existing(connection: sqlite3.Connection) -> dict[str, tuple[int, int, str]]:
    rows = connection.execute(
        "SELECT entry_key, file_size, modified_time, fingerprint FROM saves_cache_entries"
    ).fetchall()
    return {row[0]: (int(row[1] or 0), int(row[2] or 0), row[3] or "") for row in rows}


def sync_saves_cache(saves_root: Path) -> dict:
    """Scan disk, reconcile against the cache, and queue created/updated/deleted changes.

    Returns a summary ``{"created", "updated", "deleted", "total", "thumbprint"}``.
    Only files whose size or modified-time changed are re-fingerprinted (the scan above
    already hashes everything; the cache comparison avoids redundant DB writes).
    """
    scanned = scan_saves(saves_root)
    scanned_by_key = {entry.entry_key: entry for entry in scanned}
    created = updated = deleted = 0
    with _open(saves_root) as connection:
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
            connection.execute("DELETE FROM saves_cache_entries WHERE entry_key = ?", (key,))
            _queue_change(connection, key, "delete")
            deleted += 1
        connection.commit()
    return {
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "total": len(scanned),
        "thumbprint": saves_inventory_thumbprint(scanned),
    }


def _upsert(connection: sqlite3.Connection, entry: SaveEntry) -> None:
    connection.execute(
        "INSERT INTO saves_cache_entries (entry_key, system, file_path, save_name, absolute_path, file_size, modified_time, fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(entry_key) DO UPDATE SET system=excluded.system, file_path=excluded.file_path, save_name=excluded.save_name, "
        "absolute_path=excluded.absolute_path, file_size=excluded.file_size, modified_time=excluded.modified_time, fingerprint=excluded.fingerprint",
        (
            entry.entry_key,
            entry.system,
            entry.file_path,
            entry.save_name,
            entry.absolute_path,
            entry.file_size,
            entry.modified_time,
            entry.fingerprint,
        ),
    )
    connection.execute("DELETE FROM deleted_saves_cache_entries WHERE entry_key = ?", (entry.entry_key,))


def _archive_deleted(connection: sqlite3.Connection, entry_key: str) -> None:
    row = connection.execute(
        "SELECT entry_key, system, file_path, save_name, absolute_path, file_size, modified_time, fingerprint "
        "FROM saves_cache_entries WHERE entry_key = ?",
        (entry_key,),
    ).fetchone()
    if not row:
        return
    connection.execute(
        "INSERT INTO deleted_saves_cache_entries (entry_key, system, file_path, save_name, absolute_path, file_size, modified_time, fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(entry_key) DO UPDATE SET "
        "system=excluded.system, file_path=excluded.file_path, save_name=excluded.save_name, absolute_path=excluded.absolute_path, "
        "file_size=excluded.file_size, modified_time=excluded.modified_time, fingerprint=excluded.fingerprint",
        row,
    )


def _queue_change(connection: sqlite3.Connection, entry_key: str, operation: str) -> None:
    connection.execute(
        "INSERT INTO saves_cache_changes (entry_key, operation) VALUES (?, ?) "
        "ON CONFLICT(entry_key) DO UPDATE SET operation=excluded.operation",
        (entry_key, operation),
    )


def list_saves(saves_root: Path, system: Optional[str] = None) -> list[dict]:
    """Return cached save rows (optionally filtered by system) as upload-ready payloads."""
    with _open(saves_root) as connection:
        if system:
            rows = connection.execute(
                "SELECT system, file_path, save_name, absolute_path, file_size, modified_time, fingerprint "
                "FROM saves_cache_entries WHERE system = ? COLLATE NOCASE ORDER BY file_path",
                (system,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT system, file_path, save_name, absolute_path, file_size, modified_time, fingerprint "
                "FROM saves_cache_entries ORDER BY system, file_path"
            ).fetchall()
    return [
        SaveEntry(
            entry_key=_entry_key(row[0], row[1]),
            system=row[0],
            file_path=row[1],
            save_name=row[2],
            absolute_path=row[3] or "",
            file_size=int(row[4] or 0),
            modified_time=int(row[5] or 0),
            fingerprint=row[6] or "",
        ).to_payload()
        for row in rows
    ]


def list_saves_page(
    saves_root: Path,
    *,
    systems: Optional[Iterable[str]] = None,
    query: str = "",
    limit: int = 500,
    offset: int = 0,
) -> dict:
    """Return a filtered save page and total directly from SQLite."""
    safe_limit = max(1, min(int(limit), 2000))
    safe_offset = max(0, int(offset))
    selected_systems = sorted(
        {str(value or "").strip().lower() for value in systems or [] if str(value or "").strip()}
    )
    normalized_query = str(query or "").strip()
    where_parts: list[str] = []
    parameters: list = []
    if selected_systems:
        placeholders = ",".join("?" for _ in selected_systems)
        where_parts.append(f"system COLLATE NOCASE IN ({placeholders})")
        parameters.extend(selected_systems)
    if normalized_query:
        escaped = normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where_parts.append(
            "(save_name COLLATE NOCASE LIKE ? ESCAPE '\\' "
            "OR file_path COLLATE NOCASE LIKE ? ESCAPE '\\' "
            "OR fingerprint COLLATE NOCASE LIKE ? ESCAPE '\\')"
        )
        parameters.extend([pattern, pattern, pattern])
    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
    columns = "system, file_path, save_name, absolute_path, file_size, modified_time, fingerprint"
    with _open(saves_root) as connection:
        total = int(connection.execute(f"SELECT COUNT(*) FROM saves_cache_entries{where}", parameters).fetchone()[0])
        rows = connection.execute(
            f"SELECT {columns} FROM saves_cache_entries{where} "
            "ORDER BY system COLLATE NOCASE, file_path COLLATE NOCASE, entry_key LIMIT ? OFFSET ?",
            [*parameters, safe_limit, safe_offset],
        ).fetchall()
    items = [
        SaveEntry(
            entry_key=_entry_key(row[0], row[1]),
            system=row[0],
            file_path=row[1],
            save_name=row[2],
            absolute_path=row[3] or "",
            file_size=int(row[4] or 0),
            modified_time=int(row[5] or 0),
            fingerprint=row[6] or "",
        ).to_payload()
        for row in rows
    ]
    return {"total": total, "limit": safe_limit, "offset": safe_offset, "items": items}


def read_pending_changes(saves_root: Path) -> dict:
    """Return queued saves changes as ``{"saves": [...], "deleted": [...]}`` payloads."""
    changes: dict = {"saves": [], "deleted": []}
    with _open(saves_root) as connection:
        for entry_key, operation in connection.execute(
            "SELECT entry_key, operation FROM saves_cache_changes ORDER BY entry_key"
        ).fetchall():
            if operation == "delete":
                row = connection.execute(
                    "SELECT system, file_path, save_name, absolute_path, file_size, modified_time, fingerprint "
                    "FROM deleted_saves_cache_entries WHERE entry_key = ?",
                    (entry_key,),
                ).fetchone()
                bucket = "deleted"
            else:
                row = connection.execute(
                    "SELECT system, file_path, save_name, absolute_path, file_size, modified_time, fingerprint "
                    "FROM saves_cache_entries WHERE entry_key = ?",
                    (entry_key,),
                ).fetchone()
                bucket = "saves"
            if not row:
                continue
            changes[bucket].append(
                SaveEntry(
                    entry_key=entry_key,
                    system=row[0],
                    file_path=row[1],
                    save_name=row[2],
                    absolute_path=row[3] or "",
                    file_size=int(row[4] or 0),
                    modified_time=int(row[5] or 0),
                    fingerprint=row[6] or "",
                ).to_payload()
            )
    return changes


def clear_pending_changes(saves_root: Path) -> None:
    with _open(saves_root) as connection:
        connection.execute("DELETE FROM saves_cache_changes")
        connection.execute("DELETE FROM deleted_saves_cache_entries")
        connection.commit()


def saves_inventory_thumbprint(entries: Iterable) -> str:
    """SHA256 over the sorted save set (system, path, fingerprint, size).

    Accepts either SaveEntry objects or upload payload dicts so callers can thumbprint
    a fresh scan or the cached rows interchangeably.
    """
    rows = []
    for entry in entries or []:
        if isinstance(entry, SaveEntry):
            system, path, fingerprint, size = entry.system, entry.file_path, entry.fingerprint, entry.file_size
        elif isinstance(entry, dict):
            system = str(entry.get("system") or entry.get("system_name") or "")
            path = _normalize_path(entry.get("file_path") or entry.get("relative_path"))
            fingerprint = str(entry.get("fingerprint") or entry.get("saves_fingerprint") or "")
            size = entry.get("file_size") or entry.get("byte_count") or 0
        else:
            continue
        system = system.strip().lower()
        path = _normalize_path(path).lower()
        if not path:
            continue
        size_value = str(int(size)) if isinstance(size, (int, float)) else str(size or "").strip()
        rows.append("\t".join((system, path, fingerprint.strip().lower(), size_value)))
    digest = hashlib.sha256()
    for value in sorted(rows):
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def stored_thumbprint(saves_root: Path) -> str:
    """Compute the thumbprint from the current cache rows (no disk re-scan)."""
    return saves_inventory_thumbprint(list_saves(saves_root))
