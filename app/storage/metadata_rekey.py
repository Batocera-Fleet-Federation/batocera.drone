"""Re-key orphaned scraped-metadata rows after a library reorganization.

Entry keys are a hash of the relative path, so moving or renaming a media file
(or its show folder) gives it a new key and leaves its scraped metadata row
pointing at a key that no longer exists -- an "orphan". The old file's
fingerprint and size survive in the ``deleted_*_cache_entries`` archive, so an
orphan can be matched back to the file that replaced it.

A row is moved only when the identity match is unambiguous: exactly one live
entry has the orphan's ``fingerprint`` + ``file_size``. Several orphans may
claim that same live entry (duplicate scrapes of one file, e.g. both a ``tmdb``
and a ``tmdb_tv`` row); one winner is chosen deterministically -- real scrapes
beat ``local`` placeholders, ``tmdb_tv`` beats ``tmdb``, then the most recent
``scraped_at`` -- and the rest are superseded (deleted) so no target is ever
shared. A ``local`` placeholder row (empty title and provider id) on the target
is not occupancy and is replaced; a real row on the target blocks the move.
Anything with no candidate or several candidate live entries is left alone so
it can be reviewed or re-scraped instead of silently attached to the wrong file.

Idempotent: a re-keyed row no longer orphans, and superseded rows are gone, so
later passes find nothing to do. The applied schema is not changed.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Iterable, NamedTuple, Optional


class OrphanClaim(NamedTuple):
    """What an orphan used to describe, plus the fields used to pick a winner."""

    fingerprint: Optional[str]
    size: int = 0
    provider: str = ""
    provider_id: str = ""
    title: str = ""
    scraped_at: str = ""


class RekeyPlan(NamedTuple):
    """``moves`` maps winning orphan keys to their live target; ``superseded``
    lists losing duplicate orphans that should be removed."""

    moves: dict[str, str]
    superseded: list[str]


def is_placeholder_row(provider: Optional[str], provider_id: Optional[str], title: Optional[str]) -> bool:
    """True for the empty ``local`` row Plex/artwork recovery writes to hold art."""
    return provider == "local" and not provider_id and not title


def _winner_rank(orphan_key: str, claim: OrphanClaim) -> tuple:
    placeholder = is_placeholder_row(claim.provider, claim.provider_id, claim.title)
    return (not placeholder, claim.provider == "tmdb_tv", claim.scraped_at or "", orphan_key)


def plan_rekeys(
    orphans: dict[str, OrphanClaim],
    live: Iterable[tuple[str, Optional[str], int]],
    occupied: set[str],
) -> RekeyPlan:
    """Return the moves and superseded duplicates for the orphans that match.

    ``orphans`` maps each orphaned metadata key to its :class:`OrphanClaim`.
    ``live`` lists every current entry as ``(entry_key, fingerprint, size)``.
    ``occupied`` holds live keys that already have a real (non-placeholder)
    metadata row and so must not receive another one.
    """
    by_identity: dict[tuple[str, int], list[str]] = defaultdict(list)
    for key, fingerprint, size in live:
        if fingerprint:
            by_identity[(fingerprint, int(size or 0))].append(key)

    claims: dict[str, list[str]] = defaultdict(list)
    for orphan_key, claim in orphans.items():
        if not claim.fingerprint:
            continue
        candidates = by_identity.get((claim.fingerprint, int(claim.size or 0)), [])
        if len(candidates) != 1:
            continue  # none, or ambiguous (duplicate copies of the same file)
        claims[candidates[0]].append(orphan_key)

    moves: dict[str, str] = {}
    superseded: list[str] = []
    for live_key, orphan_keys in claims.items():
        if live_key in occupied:
            continue
        ranked = sorted(orphan_keys, key=lambda key: _winner_rank(key, orphans[key]), reverse=True)
        moves[ranked[0]] = live_key
        superseded.extend(ranked[1:])
    return RekeyPlan(moves=moves, superseded=sorted(superseded))


def rekey_orphan_metadata(
    connection: sqlite3.Connection,
    *,
    cache_table: str,
    deleted_table: str,
    metadata_table: str,
) -> dict:
    """Move unambiguous orphan metadata rows onto their replacement entries.

    Table names are internal constants from the calling store, never user
    input. Returns ``{"orphaned", "rekeyed", "superseded", "unmatched"}`` so
    callers can report how many orphans remain.
    """
    orphan_rows = connection.execute(
        f"SELECT m.entry_key, d.fingerprint, d.file_size, m.provider, m.provider_id, m.title, m.scraped_at "
        f"FROM {metadata_table} m "
        f"LEFT JOIN {deleted_table} d ON d.entry_key = m.entry_key "
        f"WHERE m.entry_key NOT IN (SELECT entry_key FROM {cache_table})"
    ).fetchall()
    orphaned = len(orphan_rows)
    if not orphaned:
        return {"orphaned": 0, "rekeyed": 0, "superseded": 0, "unmatched": 0}

    orphans = {
        row[0]: OrphanClaim(row[1], int(row[2] or 0), row[3] or "", row[4] or "", row[5] or "", row[6] or "")
        for row in orphan_rows
        if row[1]
    }
    live = connection.execute(
        f"SELECT entry_key, fingerprint, file_size FROM {cache_table}"
    ).fetchall()
    occupied = {
        row[0]
        for row in connection.execute(f"SELECT entry_key, provider, provider_id, title FROM {metadata_table}").fetchall()
        if not is_placeholder_row(row[1], row[2], row[3])
    }
    plan = plan_rekeys(orphans, live, occupied)
    for old_key in plan.superseded:
        connection.execute(f"DELETE FROM {metadata_table} WHERE entry_key = ?", (old_key,))
    for new_key in plan.moves.values():
        # Only a placeholder can sit on a target here; real rows were excluded above.
        connection.execute(f"DELETE FROM {metadata_table} WHERE entry_key = ?", (new_key,))
    for old_key, new_key in plan.moves.items():
        connection.execute(
            f"UPDATE {metadata_table} SET entry_key = ? WHERE entry_key = ?",
            (new_key, old_key),
        )
    rekeyed = len(plan.moves)
    superseded = len(plan.superseded)
    return {
        "orphaned": orphaned,
        "rekeyed": rekeyed,
        "superseded": superseded,
        "unmatched": orphaned - rekeyed - superseded,
    }
