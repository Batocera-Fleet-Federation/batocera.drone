"""Duplicate-game detection for the Systems Browse page, as a mixin.

No-Intro/TOSEC/Redump-style ROM names carry every piece of release metadata
(region, revision, language, scene flags) in parenthesized/bracketed tags
after the real title -- "Super Mario World (USA) (Rev 1)",
"Chrono Trigger (Japan) (En,Fr,De,Es,It)". Stripping every such tag reliably
recovers a comparable base title with no per-system special-casing needed;
the same stripped-out tag text is then re-examined (separately) to rank
which copy is the "latest" one to keep -- an explicit revision/version tag
first (the strongest signal one release supersedes another), then a fixed
region-priority order as a tiebreak for otherwise-identical, never-revised
copies (e.g. plain "(USA)" vs "(Europe)").
"""

from __future__ import annotations

import re
from typing import List, Optional

try:
    from ..storage.rom_metadata_store import list_rom_cache_page
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage.rom_metadata_store import list_rom_cache_page  # type: ignore

_BRACKETED_RE = re.compile(r"[\[\(]([^\[\]()]*)[\])]")
_WHITESPACE_RE = re.compile(r"\s+")
_REVISION_RE = re.compile(r"\brev\s*([0-9]+|[a-z])\b", re.IGNORECASE)
_VERSION_RE = re.compile(r"\bv([0-9]+(?:\.[0-9]+)*)\b", re.IGNORECASE)

# Lower index = kept over a lower-priority region when two copies are
# otherwise identical (same/no revision) -- a reasonable default ordering,
# not meant to be exhaustive; anything not listed here just ranks last,
# same as no region tag at all.
_REGION_PRIORITY = [
    "world", "usa", "usa, europe", "europe, usa", "europe", "japan, usa",
    "usa, japan", "japan", "asia", "australia", "canada", "brazil", "china",
    "france", "germany", "italy", "korea", "netherlands", "russia", "spain",
    "sweden", "taiwan", "uk",
]


def normalize_rom_title(rom_name: str) -> str:
    """Strip every parenthesized/bracketed tag and collapse whitespace, for
    grouping different releases of the same game together. Deliberately the
    same "strip anything in brackets" approach movies/filename_parser.py
    uses for its own aggressive-fallback candidate -- release metadata for
    both domains lives in the same kind of tag, just a different vocabulary."""
    stripped = _BRACKETED_RE.sub(" ", str(rom_name or ""))
    return _WHITESPACE_RE.sub(" ", stripped).strip().lower()


def _revision_rank(tags_text: str) -> tuple:
    version_match = _VERSION_RE.search(tags_text)
    if version_match:
        return (2,) + tuple(int(part) for part in version_match.group(1).split("."))
    rev_match = _REVISION_RE.search(tags_text)
    if rev_match:
        value = rev_match.group(1)
        if value.isdigit():
            return (1, int(value))
        return (1, -100 + ord(value.upper()))  # any letter revision ranks below every numeric "Rev N"
    return (0,)


def _region_rank(tags_text: str) -> int:
    tags = [tag.strip().lower() for tag in _BRACKETED_RE.findall(f"({tags_text})")] if tags_text else []
    for tag in tags:
        for index, region in enumerate(_REGION_PRIORITY):
            if tag == region:
                return len(_REGION_PRIORITY) - index
    return 0


def rom_version_rank(rom_name: str) -> tuple:
    """A comparable "which copy is the definitive one to keep" score for one
    ROM's raw (un-normalized) name -- higher wins. Revision/version info is
    checked first since it's a strictly stronger signal than region alone;
    region preference only breaks a tie when revision info is identical."""
    tags_text = " ".join(_BRACKETED_RE.findall(str(rom_name or "")))
    return (_revision_rank(tags_text), _region_rank(tags_text))


class RomDuplicatesMixin:
    def find_duplicate_roms(self, *, systems=None, genre: str = "", query: str = "") -> List[dict]:
        """Group ROMs (within the given System/Category/search filters --
        the same filters the Browse grid itself uses) by (system, normalized
        title) and return only groups with more than one member. Each
        group's items are sorted best-to-worst by rom_version_rank, with the
        first item flagged ``recommended_keep`` -- the duplicate-cleanup
        UI's default selection is everything *except* that one."""
        if self.settings is None:
            return []
        page = list_rom_cache_page(self.settings, systems=systems, genre=genre, query=query, limit=5000, offset=0)
        if page is None:
            return []
        groups: dict = {}
        for item in page.get("items") or []:
            if not isinstance(item, dict):
                continue
            system = str(item.get("system") or "")
            rom_name = str(item.get("rom_name") or "")
            normalized = normalize_rom_title(rom_name)
            if not system or not normalized:
                continue
            key = (system, normalized)
            groups.setdefault(key, []).append(item)
        result = []
        for (system, normalized), items in groups.items():
            if len(items) < 2:
                continue
            ranked = sorted(items, key=lambda entry: rom_version_rank(entry.get("rom_name") or ""), reverse=True)
            entries = []
            for index, item in enumerate(ranked):
                entries.append({
                    "system": item.get("system"),
                    "unique_id": item.get("unique_id"),
                    "rom_name": item.get("rom_name"),
                    "byte_count": item.get("byte_count") or item.get("file_size"),
                    "recommended_keep": index == 0,
                })
            result.append({"system": system, "normalized_title": normalized, "items": entries})
        result.sort(key=lambda group: (group["system"], group["normalized_title"]))
        return result
