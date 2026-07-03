"""RomRepository system-listing + search + gamelist-read methods, as a mixin.

Extracted from ``drone_api.py``. Resolves system directories, lists system names/details,
builds the in-memory search index + searches ROMs, and reads/finds ``<game>`` entries in a
system's gamelist.xml. Composed onto ``RomRepository`` (methods stay ``self``-bound).
"""

import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from ..common.http_cache import valid_segment
    from ..storage.rom_metadata_store import _read_sqlite_asset_systems, rom_cache_has_entries, search_rom_entries
    from .gamelist import _normalize_gamelist_rom_path, _text_or_empty
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.http_cache import valid_segment  # type: ignore
    from storage.rom_metadata_store import _read_sqlite_asset_systems, rom_cache_has_entries, search_rom_entries  # type: ignore
    from roms.gamelist import _normalize_gamelist_rom_path, _text_or_empty  # type: ignore


class RomSystemsSearchMixin:
    def get_system_dir(self, system: str) -> Path:
        system = valid_segment(system)
        system_link = self.roms_root / system

        if not system_link.exists():
            raise FileNotFoundError()
        if not (system_link.is_dir() or system_link.is_symlink()):
            raise ValueError("system is not a directory")

        system_dir = system_link.resolve()
        if not system_dir.exists():
            raise FileNotFoundError()
        if not system_dir.is_dir():
            raise ValueError("system target is not a directory")

        return system_dir

    def list_system_names(self) -> List[str]:
        """List usable ROM system directories without walking their content."""
        if not self.roms_root.exists():
            raise FileNotFoundError(str(self.roms_root))
        names = []
        for entry in sorted(self.roms_root.iterdir(), key=lambda p: p.name.lower()):
            if not (entry.is_dir() or entry.is_symlink()) or not self.should_include_system(entry.name):
                continue
            target_dir = entry.resolve()
            if target_dir.exists() and target_dir.is_dir():
                names.append(entry.name)
        return names

    def list_systems(self) -> List[dict]:
        cached_systems = _read_sqlite_asset_systems(self.roms_root.parent)
        if cached_systems:
            return [
                row for row in cached_systems
                if self.should_include_system(str(row.get("name") or ""))
            ]
        cached = self._cached_asset_snapshot()
        if cached:
            counts: Dict[str, int] = {}
            for row in cached.get("roms") or []:
                system = str(row.get("system") or row.get("system_name") or "").strip()
                if system:
                    counts[system] = counts.get(system, 0) + 1
            if counts:
                return [{"name": name, "rom_count": counts[name]} for name in sorted(counts, key=str.lower)]
        systems = []
        for system_name in self.list_system_names():
            target_dir = self.get_system_dir(system_name)
            rom_count = self._count_rom_items(system_name, target_dir)
            if rom_count < 1:
                continue

            systems.append({"name": system_name, "rom_count": rom_count})

        return systems

    def _build_search_index(self) -> List[dict]:
        cached = self._cached_asset_snapshot()
        if cached:
            index = []
            for rom in cached.get("roms") or []:
                system_name = str(rom.get("system") or rom.get("system_name") or "").strip()
                if not system_name or not self.should_include_system(system_name):
                    continue
                index.append(
                    {
                        "system": system_name,
                        "name": rom.get("rom_name") or rom.get("name") or rom.get("file_path") or "",
                        "unique_id": rom.get("unique_id", ""),
                        "is_downloadable": rom.get("is_downloadable", True),
                        "image_stem": rom.get("image_stem"),
                        "fingerprint": rom.get("fingerprint") or rom.get("rom_fingerprint"),
                    }
                )
            return index
        if not self.roms_root.exists():
            return []
        index: List[dict] = []
        for entry in sorted(self.roms_root.iterdir(), key=lambda p: p.name.lower()):
            if not (entry.is_dir() or entry.is_symlink()):
                continue
            if not self.should_include_system(entry.name):
                continue
            system_name = entry.name
            try:
                _, roms = self.list_assets(system_name, "roms")
            except Exception:
                continue
            for rom in roms:
                index.append(
                    {
                        "system": system_name,
                        "name": rom.get("name", ""),
                        "unique_id": rom.get("unique_id", ""),
                        "is_downloadable": rom.get("is_downloadable", True),
                        "image_stem": rom.get("image_stem"),
                        "fingerprint": rom.get("fingerprint") or rom.get("rom_fingerprint"),
                    }
                )
        return index

    def search_roms(self, query: str, limit: Optional[int] = None, system_filter: Optional[str] = None) -> List[dict]:
        normalized = query.strip()
        if not normalized:
            return []

        # Fast path: search SQLite directly (FTS5 trigram, or indexed LIKE fallback).
        # No full-cache materialization or per-query linear scan. Used whenever the
        # relational cache is populated; otherwise we fall back to the legacy index.
        if self.settings is not None and rom_cache_has_entries(self.settings):
            rows = search_rom_entries(self.settings, normalized, system_filter=system_filter)
            results = [item for item in rows if self.should_include_system(item["system"])]
            if limit is not None and limit > 0:
                return results[:limit]
            return results

        # Legacy fallback: in-memory index built from the snapshot/filesystem.
        normalized_lower = normalized.lower()
        normalized_system_filter = system_filter.strip().lower() if system_filter else None
        with self._search_cache_lock:
            now = time.time()
            if now >= self._search_index_expires_at:
                self._search_index = self._build_search_index()
                self._search_index_expires_at = now + self.rom_search_cache_ttl_seconds
            source = list(self._search_index)

        results = []
        for item in source:
            if normalized_lower not in item["name"].lower():
                continue
            if normalized_system_filter and item["system"].lower() != normalized_system_filter:
                continue
            results.append(item)
        results.sort(key=lambda item: (item["system"].lower(), item["name"].lower()))
        if limit is not None and limit > 0:
            return results[:limit]
        return results

    def _read_gamelist(self, system_dir: Path) -> Tuple[ET.ElementTree, ET.Element]:
        gamelist_path = system_dir / "gamelist.xml"
        if gamelist_path.exists() and gamelist_path.is_file():
            try:
                tree = ET.parse(gamelist_path)
                root = tree.getroot()
                if root.tag != "gameList":
                    raise ValueError("gamelist root is not gameList")
                return tree, root
            except ET.ParseError as error:
                raise ValueError(f"invalid gamelist.xml: {error}") from error
        root = ET.Element("gameList")
        return ET.ElementTree(root), root

    def _find_gamelist_entry(self, root: ET.Element, rom_name: str, rom_display_name: str) -> Optional[ET.Element]:
        normalized_file = rom_name.lower()
        normalized_file_stem = Path(rom_name).stem.lower()
        normalized_display = rom_display_name.lower()
        for game in root.findall("game"):
            path_value = _text_or_empty(game, "path")
            if path_value:
                path_name = Path(path_value.replace("\\", "/")).name.lower()
                if path_name == normalized_file or Path(path_name).stem.lower() in (normalized_file_stem, normalized_display):
                    return game
            name_value = _text_or_empty(game, "name").lower()
            if name_value and name_value in (normalized_display, normalized_file, normalized_file_stem):
                return game
        return None

    def _find_gamelist_entry_by_path(self, root: ET.Element, rom_path: str) -> Optional[ET.Element]:
        normalized = _normalize_gamelist_rom_path(rom_path).lower()
        if not normalized:
            return None
        for game in root.findall("game"):
            path_value = _normalize_gamelist_rom_path(_text_or_empty(game, "path")).lower()
            if path_value == normalized:
                return game
        return None

    # artwork-query + gamelist-entry-CRUD + rom-find methods now live in the
    # RomArtworkGamelistMixin (roms/rom_artwork_gamelist.py), composed onto RomRepository.

    # apply_remote/launchbox/thegamesdb/mobygames_artwork now live in the
    # RomArtworkApplyMixin (roms/rom_artwork_apply.py), composed onto RomRepository.
