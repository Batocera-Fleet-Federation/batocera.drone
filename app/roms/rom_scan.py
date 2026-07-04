"""RomRepository ROM filesystem-listing methods, as a mixin.

Extracted from ``drone_api.py``. Walks a system's ROM directory into item dicts
(optionally fingerprinted), counts them, attaches gamelist metadata to the items, and
produces the combined gamelist+filesystem ROM metadata. Composed onto ``RomRepository``
so the methods stay ``self``-bound with call sites unchanged.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from ..common.http_cache import valid_segment
    from .gamelist import (
        ARTWORK_FIELDS,
        _gamelist_details,
        _gamelist_game_id,
        _normalize_gamelist_rom_path,
        _text_or_empty,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.http_cache import valid_segment  # type: ignore
    from roms.gamelist import (  # type: ignore
        ARTWORK_FIELDS,
        _gamelist_details,
        _gamelist_game_id,
        _normalize_gamelist_rom_path,
        _text_or_empty,
    )


class RomScanMixin:
    def _list_rom_items(self, system: str, asset_dir: Path, include_fingerprint: bool = True) -> List[dict]:
        items: List[dict] = []
        system_lower = system.lower()

        if not asset_dir.exists() or not asset_dir.is_dir():
            return items

        if system_lower in ("ps3", "ps4"):
            for entry in sorted(asset_dir.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_file():
                    if system_lower == "steam" and entry.suffix.lower() == ".sh":
                        continue
                    if self.should_ignore_rom_file(entry.name, system=system):
                        continue
                    stat = entry.stat()
                    items.append(
                        {
                            "unique_id": self.build_unique_id(entry),
                            "name": entry.name,
                            "rom_file": entry.name,
                            "byte_count": stat.st_size,
                            "entry_type": "file",
                            "is_downloadable": True,
                            "image_stem": Path(entry.name).stem,
                        }
                    )
                    continue

                if not entry.is_dir():
                    continue

                if system_lower == "ps3":
                    if not entry.name.lower().endswith(".ps3"):
                        continue
                    size, mtime = self.build_directory_stats(entry)
                    display_name = entry.name[:-4]
                    items.append(
                        {
                            "unique_id": self.build_unique_id(entry),
                            "name": display_name,
                            "rom_file": entry.name,
                            "filename": entry.name,
                            "relative_path": entry.name,
                            "absolute_path": str(entry.resolve()),
                            "rom_path": entry.name,
                            "file_path": entry.name,
                            "byte_count": size,
                            "size": size,
                            "file_size": size,
                            "modified_time": mtime,
                            "mtime": mtime,
                            "entry_type": "folder",
                            "is_downloadable": False,
                            "source_folder": entry.name,
                            "source": "disk",
                            "metadata_source": None,
                            "image_stem": display_name,
                        }
                    )
                    continue

                # PS4: each game is a folder and includes a ".ps4" marker file for game name.
                ps4_name_file = None
                for child in sorted(entry.iterdir(), key=lambda p: p.name.lower()):
                    if child.is_file() and child.name.lower().endswith(".ps4"):
                        ps4_name_file = child
                        break
                if not ps4_name_file:
                    continue

                size, mtime = self.build_directory_stats(entry)
                display_name = ps4_name_file.stem
                items.append(
                    {
                        "unique_id": self.build_unique_id(entry),
                        "name": display_name,
                        "rom_file": entry.name,
                        "filename": entry.name,
                        "relative_path": entry.name,
                        "absolute_path": str(entry.resolve()),
                        "rom_path": entry.name,
                        "file_path": entry.name,
                        "byte_count": size,
                        "size": size,
                        "file_size": size,
                        "modified_time": mtime,
                        "mtime": mtime,
                        "entry_type": "folder",
                        "is_downloadable": False,
                        "source_folder": entry.name,
                        "source": "disk",
                        "metadata_source": None,
                        "image_stem": display_name,
                    }
                )
            return items

        for entry in sorted(asset_dir.rglob("*"), key=lambda p: p.relative_to(asset_dir).as_posix().lower()):
            if not entry.is_file():
                continue
            relative_path = entry.relative_to(asset_dir).as_posix()
            if self.should_ignore_rom_path(Path(relative_path)):
                continue
            display_name = Path(entry.name).stem
            stat = entry.stat()
            item = {
                "unique_id": self.build_unique_id(entry),
                "name": display_name,
                "rom_file": entry.name,
                "filename": entry.name,
                "relative_path": relative_path,
                "absolute_path": str(entry.resolve()),
                "rom_path": relative_path,
                "file_path": relative_path,
                "byte_count": stat.st_size,
                "size": stat.st_size,
                "file_size": stat.st_size,
                "modified_time": int(stat.st_mtime),
                "mtime": int(stat.st_mtime),
                "source": "disk",
                "metadata_source": None,
                "entry_type": "file",
                "is_downloadable": (system_lower != "steam"),
                "image_stem": display_name,
            }
            if include_fingerprint:
                fingerprint_value = self.build_fingerprint(entry)
                item["fingerprint"] = fingerprint_value
                item["rom_fingerprint"] = fingerprint_value
            items.append(item)
        return items

    def _count_rom_items(self, system: str, asset_dir: Path) -> int:
        system_lower = system.lower()
        if not asset_dir.exists() or not asset_dir.is_dir():
            return 0

        if system_lower in ("ps3", "ps4"):
            count = 0
            for entry in sorted(asset_dir.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_file():
                    if system_lower == "steam" and entry.suffix.lower() == ".sh":
                        continue
                    if self.should_ignore_rom_file(entry.name, system=system):
                        continue
                    count += 1
                    continue

                if not entry.is_dir():
                    continue

                if system_lower == "ps3":
                    if entry.name.lower().endswith(".ps3"):
                        count += 1
                    continue

                for child in sorted(entry.iterdir(), key=lambda p: p.name.lower()):
                    if child.is_file() and child.name.lower().endswith(".ps4"):
                        count += 1
                        break
            return count

        count = 0
        for entry in sorted(asset_dir.rglob("*"), key=lambda p: p.relative_to(asset_dir).as_posix().lower()):
            if not entry.is_file():
                continue
            relative_path = entry.relative_to(asset_dir).as_posix()
            if self.should_ignore_rom_path(Path(relative_path)):
                continue
            count += 1
        return count

    def _attach_gamelist_to_rom_items(self, system_dir: Path, items: List[dict]) -> List[dict]:
        try:
            _, root = self._read_gamelist(system_dir)
        except Exception:
            root = ET.Element("gameList")
        exact_paths = {}
        path_names = {}
        path_stems = {}
        display_names = {}
        for index, game in enumerate(root.findall("game")):
            path_value = _normalize_gamelist_rom_path(_text_or_empty(game, "path")).lower()
            if path_value:
                exact_paths.setdefault(path_value, game)
                path_name = Path(path_value).name.lower()
                path_names.setdefault(path_name, (index, game))
                path_stems.setdefault(Path(path_name).stem.lower(), (index, game))
            display_name = _text_or_empty(game, "name").lower()
            if display_name:
                display_names.setdefault(display_name, (index, game))
        for item in items:
            rom_file = str(item.get("rom_file") or item.get("name") or "")
            display_name = str(item.get("image_stem") or item.get("name") or "")
            relative_path = str(item.get("relative_path") or item.get("rom_path") or rom_file)
            normalized_path = _normalize_gamelist_rom_path(relative_path).lower()
            game = exact_paths.get(normalized_path)
            if game is None:
                normalized_file = rom_file.lower()
                normalized_file_stem = Path(rom_file).stem.lower()
                normalized_display = display_name.lower()
                candidates = [
                    path_names.get(normalized_file),
                    path_stems.get(normalized_file_stem),
                    path_stems.get(normalized_display),
                    display_names.get(normalized_display),
                    display_names.get(normalized_file),
                    display_names.get(normalized_file_stem),
                ]
                matches = [candidate for candidate in candidates if candidate is not None]
                if matches:
                    game = min(matches, key=lambda candidate: candidate[0])[1]
            item["rom_path"] = relative_path
            item["title"] = _text_or_empty(game, "name") if game is not None else str(item.get("name") or display_name)
            item["existing"] = {field: _text_or_empty(game, field) if game is not None else "" for field in ARTWORK_FIELDS}
            item["gamelist"] = _gamelist_details(game)
            item["has_gamelist_entry"] = game is not None
            item["metadata_source"] = "gamelist.xml" if game is not None else item.get("metadata_source")
        return items

    def list_gamelist_rom_metadata(self, system: str, system_dir: Optional[Path] = None) -> Tuple[dict, List[dict]]:
        """Build ROM metadata from gamelist.xml, statting only referenced ROM paths."""
        system = valid_segment(system)
        system_dir = (system_dir or self.get_system_dir(system)).resolve()
        gamelist_path = system_dir / "gamelist.xml"
        tree, root = self._read_gamelist(system_dir)
        del tree
        gamelist_stat = gamelist_path.stat() if gamelist_path.exists() and gamelist_path.is_file() else None
        items: List[dict] = []
        seen_paths = set()
        system_lower = system.lower()
        for game in root.findall("game"):
            relative_path = _normalize_gamelist_rom_path(_text_or_empty(game, "path"))
            if not relative_path:
                continue
            normalized_key = relative_path.lower()
            if normalized_key in seen_paths:
                continue
            seen_paths.add(normalized_key)
            rom_path = (system_dir / relative_path).resolve()
            try:
                rom_path.relative_to(system_dir)
            except ValueError:
                continue
            if not rom_path.exists():
                continue
            if rom_path.is_dir():
                size, mtime = self.build_directory_stats(rom_path)
                entry_type = "folder"
                is_downloadable = False
            elif rom_path.is_file():
                stat = rom_path.stat()
                size = int(stat.st_size)
                mtime = int(stat.st_mtime)
                entry_type = "file"
                is_downloadable = system_lower != "steam"
            else:
                continue
            display_name = Path(relative_path).stem
            title = _text_or_empty(game, "name") or display_name
            item = {
                "unique_id": self.build_unique_id(rom_path),
                "name": title,
                "rom_name": title,
                "title": title,
                "rom_file": Path(relative_path).name,
                "filename": Path(relative_path).name,
                "relative_path": relative_path,
                "absolute_path": str(rom_path),
                "rom_path": relative_path,
                "file_path": relative_path,
                "byte_count": size,
                "size": size,
                "file_size": size,
                "modified_time": mtime,
                "mtime": mtime,
                "source": "gamelist.xml",
                "metadata_source": "gamelist.xml",
                "entry_type": entry_type,
                "is_downloadable": is_downloadable,
                "image_stem": display_name,
                "existing": {field: _text_or_empty(game, field) for field in ARTWORK_FIELDS},
                "gamelist": _gamelist_details(game),
                "gamelist_path": str(gamelist_path),
                "gamelist_game_id": _gamelist_game_id(game, relative_path),
                "has_gamelist_entry": True,
            }
            items.append(item)
        # Strict gamelist-as-source-of-truth: only <game> entries are tracked. ROM files
        # present on disk but absent from gamelist.xml are intentionally NOT supplemented
        # here -- this keeps the per-system gamelist.xml MD5 a complete change signal (a
        # ROM that isn't in the gamelist can't change what we report). A system with no
        # gamelist.xml therefore reports zero games.
        items.sort(key=lambda item: str(item.get("relative_path") or "").lower())
        gamelist = {
            "system": system,
            "system_name": system,
            "path": str(gamelist_path),
            "file_path": str(gamelist_path),
            "exists": bool(gamelist_stat),
            "rom_count": len(items),
        }
        if gamelist_stat:
            gamelist["file_size"] = int(gamelist_stat.st_size)
            gamelist["modified_time"] = int(gamelist_stat.st_mtime)
        return gamelist, items
