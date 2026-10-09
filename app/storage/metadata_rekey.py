"""Re-key orphaned scraped-metadata rows after a library reorganization.

Entry keys are a hash of the relative path, so moving or renaming a media file
(or its show folder) gives it a new key and leaves its scraped metadata row
pointing at a key that no longer exists -- an "orphan". The old file's
fingerprint and size survive in the ``deleted_*_cache_entries`` archive, so an
orphan can be matched back to the file that replaced it.

A row is moved only when the match is unambiguous: exactly one live entry has
the orphan's ``fingerprint`` + ``file_size``, that live entry has no metadata
row of its own yet, and no other orphan claims the same live entry. Anything
else (multiple candidates, no candidate, a collision) is left alone so it can
be reviewed or re-scraped instead of silently attached to the wrong file.

Idempotent: a re-keyed row no longer orphans, so later passes find nothing to
do. The applied schema is not changed.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Iterable, Optional


def plan_rekeys(
    orphans: dict[str, tuple[Optional[str], int]],
    live: Iterable[tuple[str, Optional[str], int]],
    occupied: set[str],
) -> dict[str, str]:
    """Return ``{orphan_key: live_key}`` for the orphans that match uniquely.

    ``orphans`` maps each orphaned metadata key to the ``(fingerprint, size)``
    of the file it used to describe. ``live`` lists every current entry as
    ``(entry_key, fingerprint, size)``. ``occupied`` holds live keys that
    already have a metadata row and so must not receive another one.
    """
    by_identity: dict[tuple[str, int], list[str]] = defaultdict(list)
    for key, fingerprint, size in live:
        if fingerprint:
            by_identity[(fingerprint, int(size or 0))].append(key)

    claims: dict[str, list[str]] = defaultdict(list)
    for orphan_key, (fingerprint, size) in orphans.items():
        if not fingerprint:
            continue
        candidates = by_identity.get((fingerprint, int(size or 0)), [])
        if len(candidates) != 1:
            continue  # none, or ambiguous (duplicate copies of the same file)
        claims[candidates[0]].append(orphan_key)

    return {
        orphan_key: live_key
        for live_key, orphan_keys in claims.items()
        if len(orphan_keys) == 1 and live_key not in occupied
        for orphan_key in orphan_keys
    }


def rekey_orphan_metadata(
    connection: sqlite3.Connection,
    *,
    cache_table: str,
    deleted_table: str,
    metadata_table: str,
) -> dict:
    """Move unambiguous orphan metadata rows onto their replacement entries.

    Table names are internal constants from the calling store, never user
    input. Returns ``{"orphaned", "rekeyed", "unmatched"}`` so callers can
    report how many orphans remain.
    """
    orphan_rows = connection.execute(
        f"SELECT m.entry_key, d.fingerprint, d.file_size FROM {metadata_table} m "
        f"LEFT JOIN {deleted_table} d ON d.entry_key = m.entry_key "
        f"WHERE m.entry_key NOT IN (SELECT entry_key FROM {cache_table})"
    ).fetchall()
    orphaned = len(orphan_rows)
    if not orphaned:
        return {"orphaned": 0, "rekeyed": 0, "unmatched": 0}

    orphans = {row[0]: (row[1], int(row[2] or 0)) for row in orphan_rows if row[1]}
    live = connection.execute(
        f"SELECT entry_key, fingerprint, file_size FROM {cache_table}"
    ).fetchall()
    occupied = {
        row[0]
        for row in connection.execute(f"SELECT entry_key FROM {metadata_table}").fetchall()
    }
    plan = plan_rekeys(orphans, live, occupied)
    for old_key, new_key in plan.items():
        connection.execute(
            f"UPDATE {metadata_table} SET entry_key = ? WHERE entry_key = ?",
            (new_key, old_key),
        )
    return {"orphaned": orphaned, "rekeyed": len(plan), "unmatched": orphaned - len(plan)}
