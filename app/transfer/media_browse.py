"""Merged, multi-Drone media browsing (movies/shows, music, games).

Pure logic -- no HTTP, no SQLite -- so it is trivially testable.  The web
mixin (``web/handlers_media_browse.py``) supplies a ``fetch`` callable that
returns one page from one source (this Drone, or a paired peer).

Paging model
------------
Sources do not share one sort key (ROMs sort by system, movies by path, ...),
so a sorted k-way merge is not possible.  Instead each page is a deterministic
**round-robin interleave**: sources are ordered (this Drone first, then peers
by Drone id), every source is asked for ``limit`` items at its own offset, and
items are dealt one per source in turn until ``limit`` items are collected.
The opaque ``cursor`` records how many items each source has contributed so far,
so the next page resumes exactly where this one stopped -- no skipped or
repeated items, the same cursor always yields the same page.  The UI sorts the
loaded set client-side.

A source that is offline, slow, or too old to know the requested type does not
fail the page: it is reported in ``sources`` with a status and contributes
nothing.  Its cursor offset is preserved, so it resumes if it comes back.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

MEDIA_TYPES = ("movies", "music", "roms")
DEFAULT_PAGE_SIZE = 200
MAX_PAGE_SIZE = 500
MAX_SOURCES = 32

STATUS_OK = "ok"
STATUS_CACHED = "cached"  # peer unreachable; served from the last good copy
STATUS_OFFLINE = "offline"
STATUS_TIMEOUT = "timeout"
STATUS_UNSUPPORTED = "unsupported"
STATUS_ERROR = "error"


class SourceUnsupported(Exception):
    """The source answered but does not know this media type (old Drone)."""


class SourceTimeout(Exception):
    """The source did not answer within its budget."""


class SourceCache:
    """Small thread-safe cache of one peer's inventory pages.

    A page load fans out to every paired Drone; without this, opening the
    browser (or paging back and forth) re-asks every Batocera machine for the
    same assets.  Entries are *fresh* for ``fresh_seconds`` (served without any
    network), and remain usable as a **stale fallback** for ``stale_seconds``
    so an unreachable peer still shows what it had instead of vanishing.
    Bounded (LRU) so a large swarm cannot grow it without limit.
    """

    def __init__(self, fresh_seconds: float = 120.0, stale_seconds: float = 24 * 3600.0, max_entries: int = 512,
                 clock=time.monotonic) -> None:
        self.fresh_seconds = float(fresh_seconds)
        self.stale_seconds = float(stale_seconds)
        self.max_entries = int(max_entries)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: "OrderedDict[tuple, tuple]" = OrderedDict()

    def get(self, key: tuple, *, allow_stale: bool = False):
        """Return ``(page, age_seconds)`` or ``None``."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            page, stored = entry
            age = self._clock() - stored
            limit = self.stale_seconds if allow_stale else self.fresh_seconds
            if age > limit:
                if age > self.stale_seconds:
                    self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return page, age

    def put(self, key: tuple, page: dict) -> None:
        with self._lock:
            self._entries[key] = (page, self._clock())
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def invalidate(self, drone_id: Optional[str] = None) -> int:
        """Drop everything (or one Drone's entries); returns how many."""
        with self._lock:
            if drone_id is None:
                count = len(self._entries)
                self._entries.clear()
                return count
            doomed = [key for key in self._entries if key and key[0] == drone_id]
            for key in doomed:
                del self._entries[key]
            return len(doomed)


@dataclass(frozen=True)
class MediaSource:
    drone_id: str
    name: str
    is_local: bool = False


def order_sources(sources: List[MediaSource]) -> List[MediaSource]:
    """Local first, then peers by Drone id; duplicates dropped; bounded."""
    seen = set()
    unique: List[MediaSource] = []
    for source in sources:
        if not source.drone_id or source.drone_id in seen:
            continue
        seen.add(source.drone_id)
        unique.append(source)
    unique.sort(key=lambda source: (not source.is_local, source.drone_id))
    return unique[:MAX_SOURCES]


def encode_cursor(offsets: Dict[str, int]) -> str:
    clean = {str(key): int(value) for key, value in offsets.items() if int(value) > 0}
    raw = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: Optional[str]) -> Dict[str, int]:
    """Tolerant decode: anything malformed is treated as 'start over'."""
    text = str(cursor or "").strip()
    if not text or len(text) > 4096:
        return {}
    try:
        padded = text + "=" * (-len(text) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    offsets: Dict[str, int] = {}
    for key, value in data.items():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            offsets[str(key)] = number
    return offsets


def clamp_limit(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return max(1, min(number, MAX_PAGE_SIZE))


def collect_page(
    sources: List[MediaSource],
    fetch: Callable[[MediaSource, int, int], dict],
    *,
    limit: int,
    cursor: Optional[str] = None,
    max_workers: int = 8,
) -> dict:
    """Fan out to every source in parallel and deal one merged page.

    ``fetch(source, offset, limit)`` returns ``{"total": int, "items": [...]}``
    or raises ``SourceUnsupported`` / ``SourceTimeout`` / any other exception
    (reported as ``offline`` for OS-level failures, else ``error``).
    """
    ordered = order_sources(sources)
    limit = clamp_limit(limit)
    offsets = decode_cursor(cursor)
    results: Dict[str, dict] = {}

    def run(source: MediaSource):
        offset = offsets.get(source.drone_id, 0)
        try:
            page = fetch(source, offset, limit)
            items = [item for item in (page.get("items") or []) if isinstance(item, dict)]
            return source.drone_id, {
                "status": STATUS_CACHED if page.get("stale") else STATUS_OK,
                "total": max(0, int(page.get("total") or 0)),
                "items": items,
                **({"age": int(page["age"])} if page.get("stale") and page.get("age") is not None else {}),
            }
        except SourceUnsupported:
            return source.drone_id, {"status": STATUS_UNSUPPORTED, "total": 0, "items": []}
        except SourceTimeout:
            return source.drone_id, {"status": STATUS_TIMEOUT, "total": 0, "items": []}
        except (OSError, ConnectionError) as error:  # URLError/ssl.SSLError are OSErrors
            return source.drone_id, {"status": STATUS_OFFLINE, "total": 0, "items": [], "error": _short(error)}
        except Exception as error:  # noqa: BLE001 - one bad source never fails the page
            return source.drone_id, {"status": STATUS_ERROR, "total": 0, "items": [], "error": _short(error)}

    if ordered:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(ordered)))) as pool:
            for drone_id, result in pool.map(run, ordered):
                results[drone_id] = result

    consumed = {source.drone_id: 0 for source in ordered}
    merged: List[dict] = []
    progressed = True
    while len(merged) < limit and progressed:
        progressed = False
        for source in ordered:
            if len(merged) >= limit:
                break
            items = results[source.drone_id]["items"]
            index = consumed[source.drone_id]
            if index < len(items):
                item = dict(items[index])
                item["drone_id"] = source.drone_id
                item["drone_name"] = source.name
                item["is_local"] = source.is_local
                merged.append(item)
                consumed[source.drone_id] = index + 1
                progressed = True

    next_offsets = dict(offsets)
    has_more = False
    summaries = []
    total = 0
    for source in ordered:
        result = results[source.drone_id]
        position = offsets.get(source.drone_id, 0) + consumed[source.drone_id]
        next_offsets[source.drone_id] = position
        if result["status"] in (STATUS_OK, STATUS_CACHED):
            total += result["total"]
            if position < result["total"]:
                has_more = True
        summaries.append({
            "drone_id": source.drone_id,
            "name": source.name,
            "is_local": source.is_local,
            "status": result["status"],
            "total": result["total"],
            "returned": consumed[source.drone_id],
            **({"error": result["error"]} if result.get("error") else {}),
            **({"cached_age_seconds": result["age"]} if "age" in result else {}),
        })
    return {
        "items": merged,
        "total": total,
        "limit": limit,
        "has_more": has_more,
        "next_cursor": encode_cursor(next_offsets) if has_more else "",
        "sources": summaries,
    }


def _short(error: BaseException) -> str:
    return str(error)[:200]


# ---------------------------------------------------------------- matching

_NOISE_RE = re.compile(r"[^a-z0-9]+")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def normalize_title(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return _NOISE_RE.sub(" ", text.lower()).strip()


def _year(item: dict) -> str:
    for key in ("year", "release_year"):
        value = str(item.get(key) or "").strip()
        if value:
            return value[:4]
    match = _YEAR_RE.search(str(item.get("display_title") or item.get("movie_name") or item.get("file_path") or ""))
    return match.group(0) if match else ""


def item_title(item: dict) -> str:
    return str(
        item.get("display_title") or item.get("title") or item.get("track_name")
        or item.get("movie_name") or item.get("rom_name") or item.get("name") or ""
    )


def item_fingerprint(item: dict) -> str:
    return str(
        item.get("fingerprint") or item.get("rom_fingerprint") or item.get("movies_fingerprint")
        or item.get("music_fingerprint") or ""
    ).strip().lower()


def same_item(kind: str, a: dict, b: dict) -> bool:
    """Do two items from different Drones look like the same title?

    Fingerprint equality wins; otherwise the normalized title must match, plus
    year for movies, artist for music, and system for games."""
    fa, fb = item_fingerprint(a), item_fingerprint(b)
    if fa and fb:
        if fa == fb:
            return True
    ta, tb = normalize_title(item_title(a)), normalize_title(item_title(b))
    if not ta or ta != tb:
        return False
    if kind == "movies":
        ya, yb = _year(a), _year(b)
        return not (ya and yb and ya != yb)
    if kind == "music":
        aa, ab = normalize_title(a.get("artist")), normalize_title(b.get("artist"))
        return not (aa and ab and aa != ab)
    if kind == "roms":
        sa, sb = normalize_title(a.get("system")), normalize_title(b.get("system"))
        return bool(sa) and sa == sb
    return False
