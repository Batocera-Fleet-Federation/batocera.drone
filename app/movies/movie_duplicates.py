"""Duplicate movie/episode detection for the Movies Browse page.

Groups by normalized title (+ year for a movie, disambiguating same-titled
remakes/reboots -- this library alone has a dozen different "Halloween"
movies spanning 1978-2022, see filename_parser.search_candidates's own
docstring) or by (show, season, episode) for a TV episode -- deliberately
precise there since filename_parser.classify already extracts those
exactly. Ranking reuses filename_parser's own resolution/source-tier
vocabulary (_SCENE_TOKENS_RE) rather than inventing a second one, with file
size as the final tiebreak.

Bonus/extra content (Featurettes etc., KIND_EXTRA) is deliberately excluded
-- duplicate detection is about "do I have two copies of the same movie/
episode to clean up", not bonus material's own quality/version.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

try:
    from ..storage import movies_store as _movies_store
    from . import filename_parser as _filename_parser
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage import movies_store as _movies_store  # type: ignore
    from movies import filename_parser as _filename_parser  # type: ignore

_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_TRAILING_GROUP_RE = re.compile(r"-[A-Za-z0-9.]{2,20}$")
_WHITESPACE_RE = re.compile(r"\s+")

_RESOLUTION_TIERS = {"2160p": 4, "4k": 4, "uhd": 4, "1080p": 3, "720p": 2, "480p": 1}
# A tagged-but-poor source (camrip/dvdscr) still ranks above no tag at all --
# an untagged release could be anything, so it shouldn't be assumed better
# than a release that at least says what it is.
_SOURCE_TIERS = {
    "bluray": 5, "brrip": 5, "bdrip": 5,
    "webdl": 4, "web-dl": 4, "webrip": 4,
    "hdtv": 3, "hdrip": 3,
    "dvdrip": 2,
    "dvdscr": 1, "camrip": 1,
}


def normalize_movie_title(movie_name: str) -> str:
    """Strip everything from the first year token onward (scene-release
    convention always places the year right after the title -- the same
    reliable cut filename_parser.search_candidates's own "year-cut
    candidate" rung uses), then any remaining bracket/scene-tag noise, for
    grouping different quality releases of the same movie together."""
    text = str(movie_name or "")
    text = Path(text).stem if "." in Path(text).name else text
    year_match = _YEAR_RE.search(text)
    if year_match:
        text = text[: year_match.start()]
    text = _filename_parser._BRACKETED_RE.sub(" ", text)
    text = _filename_parser._SCENE_TOKENS_RE.sub(" ", text)
    text = _filename_parser._PUNCTUATION_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def movie_year(movie_name: str) -> Optional[str]:
    match = _YEAR_RE.search(str(movie_name or ""))
    return match.group(1) if match else None


def movie_quality_rank(movie_name: str, byte_count: Optional[int]) -> tuple:
    """A comparable "which copy is the better release to keep" score --
    higher wins. Resolution is checked first, then source tier, then file
    size as a last-resort tiebreak between two releases with identical
    resolution/source tags."""
    text = str(movie_name or "").lower()
    resolution_rank = 0
    for tag, rank in _RESOLUTION_TIERS.items():
        if re.search(rf"\b{re.escape(tag)}\b", text) and rank > resolution_rank:
            resolution_rank = rank
    source_rank = 0
    for tag, rank in _SOURCE_TIERS.items():
        if re.search(rf"\b{re.escape(tag)}\b", text) and rank > source_rank:
            source_rank = rank
    return (resolution_rank, source_rank, int(byte_count or 0))


def find_duplicate_movies(movies_root: Path, *, kind_filter: str = "", genre: str = "", query: str = "") -> List[dict]:
    """Group movies/episodes by normalized title(+year)/show+season+episode
    and return only groups with more than one member. Each group's items
    are sorted best-to-worst by movie_quality_rank, with the first item
    flagged ``recommended_keep`` -- the duplicate-cleanup UI's default
    selection is everything *except* that one. ``kind_filter``/``genre``/
    ``query`` mirror the Movies Explorer sidebar's own Type/Genre/search
    filters."""
    rows = _movies_store.list_movies(movies_root)
    genres_by_key = _movies_store.list_movie_genres(movies_root)
    display_titles_by_key = _movies_store.list_movie_display_titles(movies_root)
    kind_filter = str(kind_filter or "").strip().lower()
    genre = str(genre or "").strip()
    query = str(query or "").strip().lower()

    groups: dict = {}
    for row in rows:
        parsed = _filename_parser.classify(row.get("file_path") or row.get("relative_path") or "", row.get("movie_name") or "")
        if parsed.kind == _filename_parser.KIND_EXTRA:
            continue
        if kind_filter and kind_filter != "all" and parsed.kind != kind_filter:
            continue
        entry_key = row.get("entry_key")
        row_genres = genres_by_key.get(entry_key) or []
        if genre and genre not in row_genres:
            continue
        display_title = display_titles_by_key.get(entry_key) or row.get("movie_name") or ""
        row["display_title"] = display_title
        if query and query not in str(display_title).lower() and query not in str(row.get("movie_name") or "").lower():
            continue

        if parsed.kind == _filename_parser.KIND_EPISODE:
            show_key = _filename_parser._collapse(re.sub(r"\s*&\s*", " and ", parsed.show_title or "", flags=re.IGNORECASE)).lower()
            if not show_key or parsed.season is None or parsed.episode is None:
                continue
            key = ("episode", show_key, parsed.season, parsed.episode)
            label = f"{parsed.show_title} S{parsed.season:02d}E{parsed.episode:02d}"
        else:
            title_key = normalize_movie_title(row.get("movie_name") or "")
            if not title_key:
                continue
            year = movie_year(row.get("movie_name") or "")
            key = ("movie", title_key, year)
            # A deterministic, title-cased label from the normalized key
            # itself -- not an arbitrary group member's raw filename (which
            # member is processed first depends on list_movies()'s row
            # order and reads as if the group were named after just that
            # one release, confusing next to its own "Keep" row below it).
            label = f"{title_key.title()} ({year})" if year else title_key.title()

        groups.setdefault(key, {"label": label, "items": []})["items"].append(row)

    result = []
    for key, group in groups.items():
        items = group["items"]
        if len(items) < 2:
            continue
        ranked = sorted(items, key=lambda entry: movie_quality_rank(entry.get("movie_name") or "", entry.get("byte_count")), reverse=True)
        entries = []
        for index, item in enumerate(ranked):
            entries.append({
                "entry_key": item.get("entry_key"),
                "movie_name": item.get("movie_name"),
                "display_title": item.get("display_title") or item.get("movie_name"),
                "byte_count": item.get("byte_count"),
                "recommended_keep": index == 0,
            })
        result.append({"kind": key[0], "label": group["label"], "items": entries})
    result.sort(key=lambda group: (group["kind"], group["label"].lower()))
    return result
