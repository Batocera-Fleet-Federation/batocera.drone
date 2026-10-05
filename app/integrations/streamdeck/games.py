"""Game picker + Launch Game resolution over the Drone's existing local library.

No second library model: search goes through ``RomRepository.search_roms``
(SQLite FTS over the ROM cache), per-system browsing and titles/favorites/
artwork references come from ``RomRepository.list_assets`` (ROM cache rows with
their gamelist.xml entry attached). A game is identified by the ROM cache's
``unique_id`` (the same ID Browse/detail URLs use) plus its system and
system-relative ROM path as recovery data, so renamed/rescanned games relink
by path and deleted games resolve to "missing" instead of launching anything.

Artwork is local only (gamelist.xml references, then the conventional
``images/<stem>-image.*`` files) -- nothing is downloaded.
"""

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from ...storage.rom_metadata_store import get_rom_cache_row
except ImportError:  # pragma: no cover - flat execution
    from storage.rom_metadata_store import get_rom_cache_row  # type: ignore

from .config import normalize_rom_path, safe_id

# Key art preference: the game's main artwork, then its logo, then box art /
# screenshots, then fan art; the generated title button is the final fallback.
ARTWORK_PREFERENCE = ("image", "marquee", "wheel", "thumbnail", "boxart", "fanart")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
_INDEX_TTL_SECONDS = 30.0
_SEARCH_ENRICH_SYSTEMS = 4


class GameLibrary:
    def __init__(self, repository: Any, roms_root: Path) -> None:
        self.repository = repository
        self.roms_root = Path(os.path.abspath(str(roms_root)))
        self._index: Dict[str, Tuple[float, List[dict]]] = {}
        self._lock = threading.Lock()

    # -- library access -------------------------------------------------------
    def systems(self) -> List[dict]:
        try:
            rows = self.repository.list_systems()
        except Exception:  # noqa: BLE001 - a broken system dir must not break the picker
            return []
        return [{"name": str(row.get("name")), "rom_count": row.get("rom_count")} for row in rows if row.get("name")]

    def _system_index(self, system: str) -> List[dict]:
        system = safe_id(system, "system")
        now = time.monotonic()
        with self._lock:
            cached = self._index.get(system)
            if cached and now - cached[0] < _INDEX_TTL_SECONDS:
                return cached[1]
        try:
            _system_dir, items = self.repository.list_assets(system, "roms", include_fingerprint=False)
        except (FileNotFoundError, ValueError):
            items = []
        with self._lock:
            self._index[system] = (now, items)
        return items

    def invalidate(self) -> None:
        with self._lock:
            self._index.clear()

    def _summary(self, system: str, item: dict) -> dict:
        rom_path = str(item.get("rom_path") or item.get("relative_path") or item.get("file_path") or "")
        details = item.get("gamelist") or {}
        favorite = details.get("favorite") if isinstance(details, dict) else ""
        existing = item.get("existing") or {}
        return {
            "id": str(item.get("unique_id") or ""),
            "name": str(item.get("title") or item.get("name") or Path(rom_path).stem),
            "system": system,
            "rom_path": rom_path,
            "rom_file": Path(rom_path).name,
            "favorite": str(favorite).strip().lower() == "true",
            "has_artwork": any(existing.get(field) for field in ARTWORK_PREFERENCE),
            "installed": True,
            "metadata_source": "drone-rom-cache",
        }

    def search(self, query: str = "", *, system: Optional[str] = None, limit: int = 30, offset: int = 0) -> dict:
        limit = min(100, max(1, int(limit)))
        offset = max(0, int(offset))
        query = str(query or "").strip()[:100]
        system = safe_id(system, "system") if system else None
        if system:
            items = [self._summary(system, item) for item in self._system_index(system)]
            if query:
                needle = query.lower()
                items = [item for item in items if needle in item["name"].lower() or needle in item["rom_file"].lower()]
            items.sort(key=lambda item: (not item["favorite"], item["name"].lower()))
            page = items[offset: offset + limit]
            return {"items": page, "total": len(items), "limit": limit, "offset": offset,
                    "has_more": offset + limit < len(items), "mode": "browse" if not query else "system-search"}
        if not query:
            return {"items": [], "total": 0, "limit": limit, "offset": offset, "has_more": False, "mode": "empty"}
        rows = self.repository.search_roms(query, limit=offset + limit + 1)
        page_rows = rows[offset: offset + limit]
        items = []
        # Titles/favorites need a system's gamelist; parse at most a few uncached
        # systems per query so a broad search stays fast on large libraries.
        enrich_budget = _SEARCH_ENRICH_SYSTEMS
        for row in page_rows:
            row_system = str(row.get("system") or "")
            unique_id = str(row.get("unique_id") or "")
            if not row_system or not unique_id:
                continue
            match = None
            cached = self._is_cached(row_system)
            if cached or enrich_budget > 0:
                if not cached:
                    enrich_budget -= 1
                match = next((item for item in self._system_index(row_system)
                              if str(item.get("unique_id")) == unique_id), None)
            if match is not None:
                items.append(self._summary(row_system, match))
                continue
            rom_path = self._cached_rom_path(row_system, unique_id)
            if rom_path:
                items.append({
                    "id": unique_id, "name": Path(str(row.get("name") or rom_path)).stem, "system": row_system,
                    "rom_path": rom_path, "rom_file": Path(rom_path).name, "favorite": False,
                    "has_artwork": False, "installed": True, "metadata_source": "drone-rom-cache",
                })
        return {"items": items, "total": None, "limit": limit, "offset": offset,
                "has_more": len(rows) > offset + limit, "mode": "search"}

    def _is_cached(self, system: str) -> bool:
        with self._lock:
            cached = self._index.get(system)
            return bool(cached and time.monotonic() - cached[0] < _INDEX_TTL_SECONDS)

    def _cached_rom_path(self, system: str, unique_id: str) -> str:
        settings = getattr(self.repository, "settings", None)
        if settings is None:
            return ""
        row = get_rom_cache_row(settings, system, unique_id) or {}
        return str(row.get("file_path") or "")

    # -- resolution ---------------------------------------------------------------
    def resolve(self, game: dict) -> dict:
        """Re-resolve a saved game reference against the current library.

        ``resolution`` is ``id`` (found by stable ID), ``path`` (ID changed --
        e.g. a rescan -- but the ROM is still at its saved path; relinked
        automatically) or ``missing`` (show "Game not found" / Relink Game).
        """
        game = dict(game or {})
        base = {"id": str(game.get("id") or ""), "name": str(game.get("name") or "Game"),
                "system": str(game.get("system") or ""), "rom_path": str(game.get("rom_path") or ""),
                "metadata_source": game.get("metadata_source") or "drone-rom-cache"}
        try:
            system = safe_id(base["system"], "system")
            saved_path = normalize_rom_path(base["rom_path"]) if base["rom_path"] else ""
        except ValueError:
            return {**base, "installed": False, "resolution": "missing", "reason": "Invalid saved game reference."}
        items = self._system_index(system)
        match = next((item for item in items if base["id"] and str(item.get("unique_id")) == base["id"]), None)
        resolution = "id"
        if match is None and saved_path:
            lowered = saved_path.lower()
            match = next((item for item in items
                          if str(item.get("rom_path") or item.get("relative_path") or "").lower() == lowered), None)
            resolution = "path"
        if match is not None:
            summary = self._summary(system, match)
            if self._exists(system, summary["rom_path"]):
                return {**summary, "resolution": resolution, "saved_id": base["id"]}
        return {**base, "installed": False, "resolution": "missing",
                "reason": "Game not found in the local library (renamed, moved, or deleted). Relink this button."}

    def _exists(self, system: str, rom_path: str) -> bool:
        try:
            system_dir = (self.roms_root / system).resolve(strict=True)
            target = (self.roms_root / system / normalize_rom_path(rom_path)).resolve(strict=True)
            target.relative_to(system_dir)
            return target.is_file() or target.is_dir()
        except (OSError, ValueError, RuntimeError):
            return False

    def artwork_path(self, game: dict, field: str = "auto") -> Optional[Path]:
        """Best local artwork file for a resolved game, confined to its system dir."""
        system = str(game.get("system") or "")
        rom_path = str(game.get("rom_path") or "")
        if not system or not rom_path:
            return None
        try:
            system_dir = (self.roms_root / safe_id(system, "system")).resolve(strict=True)
            normalized = normalize_rom_path(rom_path).lower()
        except (OSError, ValueError, RuntimeError):
            return None
        item = next((row for row in self._system_index(system)
                     if str(row.get("rom_path") or row.get("relative_path") or "").lower() == normalized), None)
        existing = (item or {}).get("existing") or {}
        fields = ARTWORK_PREFERENCE if field in ("", "auto", None) else (field,)
        for name in fields:
            reference = str(existing.get(name) or "").strip().replace("\\", "/")
            while reference.startswith("./"):
                reference = reference[2:]
            if not reference or reference.startswith("/") or Path(reference).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            candidate = self._confined(system_dir, system_dir / reference)
            if candidate is not None:
                return candidate
        if field not in ("", "auto", None):
            return None
        stem = Path(rom_path).stem
        for pattern in ("{stem}-image", "{stem}-thumb", "{stem}-marquee", "{stem}"):
            for suffix in IMAGE_SUFFIXES:
                candidate = self._confined(system_dir, system_dir / "images" / (pattern.format(stem=stem) + suffix))
                if candidate is not None:
                    return candidate
        return None

    def available_artwork(self, game: dict) -> List[str]:
        return [name for name in ARTWORK_PREFERENCE if self.artwork_path(game, name) is not None]

    @staticmethod
    def _confined(system_dir: Path, candidate: Path) -> Optional[Path]:
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(system_dir)
        except (OSError, ValueError, RuntimeError):
            return None
        return resolved if resolved.is_file() else None
