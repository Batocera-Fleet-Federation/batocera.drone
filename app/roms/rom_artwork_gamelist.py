"""RomRepository artwork-query + gamelist-entry-CRUD + rom-find methods, as a mixin.

Extracted from ``drone_api.py``. Lists/resolves artwork, reports missing/duplicate
artwork (from gamelists + filesystem), reads/writes gamelist ``<game>`` entries
(update/remove/artwork-reference), and finds ROMs by unique-id/path. Composed onto
``RomRepository`` so the methods stay ``self``-bound with call sites unchanged.
"""

import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from ..common.runtime_state import _GAMELIST_WRITE_LOCK
    from .gamelist import (
        ARTWORK_DUPLICATE_FILTER,
        ARTWORK_FIELDS,
        _artwork_identity,
        _find_gamelist_entry_by_game_id,
        _gamelist_details,
        _normalize_gamelist_rom_path,
        _remove_child,
        _set_child_text,
        _text_or_empty,
    )
    from .rom_transfer_unit import gamelist_folder_entry_counts, resolve_transfer_unit
    from .scrapers import _clean_rom_title
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.runtime_state import _GAMELIST_WRITE_LOCK  # type: ignore
    from roms.gamelist import (  # type: ignore
        ARTWORK_DUPLICATE_FILTER,
        ARTWORK_FIELDS,
        _artwork_identity,
        _find_gamelist_entry_by_game_id,
        _gamelist_details,
        _normalize_gamelist_rom_path,
        _remove_child,
        _set_child_text,
        _text_or_empty,
    )
    from roms.rom_transfer_unit import gamelist_folder_entry_counts, resolve_transfer_unit  # type: ignore
    from roms.scrapers import _clean_rom_title  # type: ignore


class RomArtworkGamelistMixin:
    def _entry_missing_artwork(self, game: Optional[ET.Element]) -> List[str]:
        missing = []
        for field in ARTWORK_FIELDS:
            if game is None or not _text_or_empty(game, field):
                missing.append(field)
        if self._entry_has_duplicate_artwork(game):
            missing.append(ARTWORK_DUPLICATE_FILTER)
        return missing

    def _entry_has_duplicate_artwork(self, game: Optional[ET.Element]) -> bool:
        if game is None:
            return False
        seen = {}
        for field in ARTWORK_FIELDS:
            identity = _artwork_identity(_text_or_empty(game, field))
            if not identity:
                continue
            if identity in seen:
                return True
            seen[identity] = field
        return False

    def list_artwork_metadata(self) -> List[dict]:
        if not self.roms_root.exists():
            return []
        rows: List[dict] = []
        for entry in sorted(self.roms_root.iterdir(), key=lambda p: p.name.lower()):
            if not (entry.is_dir() or entry.is_symlink()):
                continue
            if not self.should_include_system(entry.name):
                continue
            system = entry.name
            system_dir = entry.resolve()
            try:
                _, root = self._read_gamelist(system_dir)
            except Exception:
                continue
            for game in root.findall("game"):
                rom_path = _normalize_gamelist_rom_path(_text_or_empty(game, "path"))
                if not rom_path:
                    continue
                artwork_types = [
                    field for field in ARTWORK_FIELDS
                    if _text_or_empty(game, field)
                ]
                if not artwork_types:
                    continue
                rows.append({
                    "asset_type": "artwork",
                    "system": system,
                    "system_name": system,
                    "rom_path": rom_path,
                    "file_path": rom_path,
                    "rom_name": Path(rom_path).name,
                    "title": _text_or_empty(game, "name") or Path(rom_path).stem,
                    "artwork_types": artwork_types,
                    "metadata_source": "gamelist.xml",
                })
        rows.sort(key=lambda item: (str(item.get("system") or "").lower(), str(item.get("rom_path") or "").lower()))
        return rows

    def resolve_artwork_file(self, system: str, rom_path: str, artwork_type: str) -> Tuple[Path, str, str]:
        system_dir = self.get_system_dir(system).resolve()
        field = str(artwork_type or "").strip()
        if field not in ARTWORK_FIELDS:
            raise ValueError("invalid artwork type")
        tree, root = self._read_gamelist(system_dir)
        game = self._find_gamelist_entry_by_path(root, rom_path)
        if game is None:
            raise FileNotFoundError()
        artwork_ref = _normalize_gamelist_rom_path(_text_or_empty(game, field))
        if not artwork_ref:
            raise FileNotFoundError()
        target = (system_dir / artwork_ref).resolve()
        if not target.exists() or not target.is_file() or (target != system_dir and system_dir not in target.parents):
            raise FileNotFoundError()
        return target, target.relative_to(system_dir).as_posix(), artwork_ref

    def resolve_rom_file_by_gamelist_id(self, system: str, gamelist_id: str) -> Tuple[Path, str, str, str]:
        """Resolve a ROM by its gamelist ``<game id>`` -> (target, relative_path,
        entry_type, marker_relative_path).

        The sender maps the gamelist id to the game's ``<path>`` in its own
        gamelist.xml (mirroring ``resolve_artwork_file``) so a receiver can pull the
        ROM without a peer ever carrying a filesystem path. Folder-unit ROMs (a
        marker/index file in a per-game folder, per ``rom_transfer_unit``) resolve to
        the FOLDER as the transfer unit with the gamelist path kept as
        ``marker_relative_path``; for everything else ``marker_relative_path`` equals
        ``relative_path``. Raises FileNotFoundError when the id is unknown or the
        resolved path escapes the system directory.
        """
        gid = str(gamelist_id or "").strip()
        if not gid:
            raise ValueError("gamelist_id is required")
        system_dir = self.get_system_dir(system).resolve()
        _, root = self._read_gamelist(system_dir)
        game = _find_gamelist_entry_by_game_id(root, gid)
        if game is None:
            raise FileNotFoundError()
        rom_ref = _normalize_gamelist_rom_path(_text_or_empty(game, "path"))
        if not rom_ref or "\x00" in rom_ref:
            raise FileNotFoundError()
        target = (system_dir / rom_ref).resolve()
        if (target != system_dir and system_dir not in target.parents) or not target.exists():
            raise FileNotFoundError()
        if target.is_file():
            unit = resolve_transfer_unit(system, rom_ref, target, system_dir, gamelist_folder_entry_counts(root))
            if unit is not None:
                return unit["unit_dir"], unit["unit_rel_path"], "folder", unit["marker_rel_path"]
        entry_type = "folder" if target.is_dir() else "file"
        relative_path = target.relative_to(system_dir).as_posix()
        return target, relative_path, entry_type, relative_path

    def list_present_artwork(self, system: str) -> Dict[str, set]:
        """Map normalized ROM path -> set of artwork fields that are both referenced
        in this system's gamelist.xml and present on disk. Lets the local-network
        sync skip re-downloading artwork that already exists, parsing the gamelist
        once per system instead of once per ROM."""
        system_dir = self.get_system_dir(system).resolve()
        try:
            _, root = self._read_gamelist(system_dir)
        except Exception:
            return {}
        present: Dict[str, set] = {}
        for game in root.findall("game"):
            rom_norm = _normalize_gamelist_rom_path(_text_or_empty(game, "path")).lower()
            if not rom_norm:
                continue
            fields = set()
            for field in ARTWORK_FIELDS:
                ref = _normalize_gamelist_rom_path(_text_or_empty(game, field))
                if not ref:
                    continue
                target = (system_dir / ref).resolve()
                if target.is_file() and (target == system_dir or system_dir in target.parents):
                    fields.add(field)
            if fields:
                present[rom_norm] = fields
        return present

    def update_gamelist_artwork_reference(self, system: str, rom_path: str, artwork_type: str, artwork_relative_path: str) -> dict:
        system_dir = self.get_system_dir(system).resolve()
        field = str(artwork_type or "").strip()
        if field not in ARTWORK_FIELDS:
            raise ValueError("invalid artwork type")
        normalized_rom_path = _normalize_gamelist_rom_path(rom_path)
        normalized_artwork_path = _normalize_gamelist_rom_path(artwork_relative_path)
        if not normalized_rom_path or not normalized_artwork_path:
            raise ValueError("rom_path and artwork path are required")
        gamelist_path = system_dir / "gamelist.xml"
        # Hold the lock across the whole read-modify-write so parallel artwork
        # downloads for this system serialize instead of overwriting each other.
        with _GAMELIST_WRITE_LOCK:
            tree, root = self._read_gamelist(system_dir)
            game = self._find_gamelist_entry_by_path(root, normalized_rom_path)
            if game is None:
                game = ET.SubElement(root, "game")
                _set_child_text(game, "path", f"./{normalized_rom_path}")
                _set_child_text(game, "name", Path(normalized_rom_path).stem)
            _set_child_text(game, field, f"./{normalized_artwork_path}")
            try:
                ET.indent(tree, space="  ")
            except Exception:
                pass
            tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
            with gamelist_path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
        return {"system": system, "rom_path": normalized_rom_path, "artwork_type": field, "artwork_path": normalized_artwork_path}

    def _rom_path_exists(self, system: str, rom_path: str) -> bool:
        try:
            system_dir = self.get_system_dir(system)
            normalized = _normalize_gamelist_rom_path(rom_path)
            if not normalized or "\x00" in normalized:
                return False
            target_path = (system_dir / normalized).resolve()
            if target_path != system_dir and system_dir not in target_path.parents:
                return False
            return target_path.exists()
        except Exception:
            return False

    def _list_missing_artwork_from_gamelists(self, include_complete: bool = False) -> List[dict]:
        if not self.roms_root.exists():
            return []
        missing_items: List[dict] = []
        for entry in sorted(self.roms_root.iterdir(), key=lambda p: p.name.lower()):
            if not (entry.is_dir() or entry.is_symlink()):
                continue
            if not self.should_include_system(entry.name):
                continue
            system = entry.name
            system_dir = entry.resolve()
            if not system_dir.exists() or not system_dir.is_dir():
                continue
            try:
                _, root = self._read_gamelist(system_dir)
            except Exception:
                continue
            for game in root.findall("game"):
                missing = self._entry_missing_artwork(game)
                if not missing and not include_complete:
                    continue
                rom_path = _normalize_gamelist_rom_path(_text_or_empty(game, "path"))
                if not rom_path:
                    continue
                rom_name = Path(rom_path).name
                title = _text_or_empty(game, "name") or Path(rom_name).stem
                missing_items.append(
                    {
                        "system": system,
                        "name": title,
                        "rom_name": rom_name,
                        "rom_path": rom_path,
                        "title": title,
                        "search_title": _clean_rom_title(title or rom_name),
                        "unique_id": "",
                        "missing": missing,
                        "existing": {field: _text_or_empty(game, field) for field in ARTWORK_FIELDS},
                        "gamelist": _gamelist_details(game),
                        "has_gamelist_entry": True,
                    }
                )
        missing_items.sort(key=lambda item: (str(item["system"]).lower(), str(item["name"]).lower()))
        return missing_items

    def _list_missing_artwork_from_filesystem(self, include_complete: bool = False) -> List[dict]:
        if not self.roms_root.exists():
            return []
        missing_items: List[dict] = []
        for entry in sorted(self.roms_root.iterdir(), key=lambda p: p.name.lower()):
            if not (entry.is_dir() or entry.is_symlink()):
                continue
            if not self.should_include_system(entry.name):
                continue
            system = entry.name
            system_dir = entry.resolve()
            if not system_dir.exists() or not system_dir.is_dir():
                continue
            try:
                _, roms = self.list_assets(system, "roms")
                _, root = self._read_gamelist(system_dir)
            except Exception:
                continue
            for rom in roms:
                rom_file = str(rom.get("rom_file") or rom.get("name") or "")
                game = self._find_gamelist_entry(root, rom_file, str(rom.get("image_stem") or rom.get("name") or ""))
                missing = self._entry_missing_artwork(game)
                if not missing and not include_complete:
                    continue
                missing_items.append(
                    {
                        "system": system,
                        "name": rom.get("name"),
                        "rom_name": rom_file,
                        "rom_path": rom_file,
                        "title": _text_or_empty(game, "name") if game is not None else str(rom.get("image_stem") or rom.get("name") or ""),
                        "search_title": _clean_rom_title(_text_or_empty(game, "name") if game is not None else str(rom.get("image_stem") or rom.get("name") or "")),
                        "unique_id": rom.get("unique_id"),
                        "missing": missing,
                        "existing": {field: _text_or_empty(game, field) if game is not None else "" for field in ARTWORK_FIELDS},
                        "gamelist": _gamelist_details(game),
                        "has_gamelist_entry": game is not None,
                    }
                )
        missing_items.sort(key=lambda item: (str(item["system"]).lower(), str(item["name"]).lower()))
        return missing_items

    def list_missing_artwork(self, include_filesystem: bool = False, force_refresh: bool = False, include_complete: bool = False) -> List[dict]:
        cache_key = f"{'filesystem' if include_filesystem else 'gamelist'}:{'all' if include_complete else 'missing'}"
        now = time.time()
        with self._missing_artwork_cache_lock:
            cached = self._missing_artwork_cache.get(cache_key)
            if cached and not force_refresh and cached.get("expires_at", 0) > now:
                return [dict(item) for item in cached.get("items", [])]

        items = (
            self._list_missing_artwork_from_filesystem(include_complete=include_complete)
            if include_filesystem
            else self._list_missing_artwork_from_gamelists(include_complete=include_complete)
        )
        with self._missing_artwork_cache_lock:
            self._missing_artwork_cache[cache_key] = {
                "items": [dict(item) for item in items],
                "expires_at": time.time() + 120,
            }
        return items

    def remove_gamelist_entry(self, system: str, rom_path: str) -> dict:
        system_dir = self.get_system_dir(system)
        normalized_rom_path = _normalize_gamelist_rom_path(rom_path)
        if not normalized_rom_path:
            raise ValueError("rom_path is required")
        # Serialize the whole read-modify-write against other gamelist writers
        # (notably the concurrent artwork-download worker) so updates aren't lost.
        with _GAMELIST_WRITE_LOCK:
            tree, root = self._read_gamelist(system_dir)
            game = self._find_gamelist_entry_by_path(root, normalized_rom_path)
            if game is None:
                raise FileNotFoundError()
            root.remove(game)
            gamelist_path = system_dir / "gamelist.xml"
            try:
                ET.indent(tree, space="  ")
            except Exception:
                pass
            tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
            with gamelist_path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
        with self._missing_artwork_cache_lock:
            self._missing_artwork_cache.clear()
        return {"system": system, "rom_path": normalized_rom_path, "removed": True}

    def remove_gamelist_entries(self, entries: List[dict]) -> dict:
        grouped: Dict[str, List[str]] = {}
        for entry in entries:
            system = str(entry.get("system") or "").strip()
            rom_path = _normalize_gamelist_rom_path(str(entry.get("rom_path") or ""))
            if not system or not rom_path:
                continue
            grouped.setdefault(system, []).append(rom_path)

        removed = []
        not_found = []
        failed = []
        for system, paths in grouped.items():
            # Hold the gamelist lock across each system's read-modify-write so a
            # concurrent writer (e.g. the artwork-download worker) can't clobber it.
            with _GAMELIST_WRITE_LOCK:
                try:
                    system_dir = self.get_system_dir(system)
                    tree, root = self._read_gamelist(system_dir)
                except Exception as error:
                    for rom_path in paths:
                        failed.append({"system": system, "rom_path": rom_path, "error": str(error)})
                    continue
                changed = False
                pending_removed = []
                for rom_path in paths:
                    game = self._find_gamelist_entry_by_path(root, rom_path)
                    if game is None:
                        not_found.append({"system": system, "rom_path": rom_path})
                        continue
                    root.remove(game)
                    pending_removed.append({"system": system, "rom_path": rom_path})
                    changed = True
                if changed:
                    gamelist_path = system_dir / "gamelist.xml"
                    try:
                        ET.indent(tree, space="  ")
                    except Exception:
                        pass
                    try:
                        tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
                        with gamelist_path.open("a", encoding="utf-8") as handle:
                            handle.write("\n")
                        removed.extend(pending_removed)
                    except Exception as error:
                        for item in pending_removed:
                            failed.append({**item, "path": str(gamelist_path), "error": str(error)})

        if removed:
            with self._missing_artwork_cache_lock:
                self._missing_artwork_cache.clear()
        return {"removed": removed, "removed_count": len(removed), "not_found": not_found, "failed": failed, "failed_count": len(failed)}

    def update_gamelist_entry(self, system: str, rom_path: str, fields: dict) -> dict:
        system_dir = self.get_system_dir(system)
        normalized_rom_path = _normalize_gamelist_rom_path(rom_path)
        if not normalized_rom_path:
            raise ValueError("rom_path is required")
        if not isinstance(fields, dict):
            raise ValueError("fields must be an object")
        # Serialize the read-modify-write against the artwork-download worker and
        # other gamelist writers so concurrent edits aren't lost.
        with _GAMELIST_WRITE_LOCK:
            tree, root = self._read_gamelist(system_dir)
            game = self._find_gamelist_entry_by_path(root, normalized_rom_path)
            created = False
            if game is None:
                game = ET.SubElement(root, "game")
                _set_child_text(game, "path", f"./{normalized_rom_path}")
                created = True

            updated = {}
            removed = []
            for raw_tag, raw_value in fields.items():
                tag = str(raw_tag or "").strip()
                if not tag or tag == "path":
                    continue
                if not re.match(r"^[A-Za-z0-9_.-]+$", tag):
                    raise ValueError(f"invalid gamelist field: {tag}")
                value = str(raw_value if raw_value is not None else "").strip()
                if value:
                    _set_child_text(game, tag, value)
                    updated[tag] = value
                else:
                    _remove_child(game, tag)
                    removed.append(tag)

            gamelist_path = system_dir / "gamelist.xml"
            try:
                ET.indent(tree, space="  ")
            except Exception:
                pass
            tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
            with gamelist_path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
        with self._missing_artwork_cache_lock:
            self._missing_artwork_cache.clear()

        return {
            "system": system,
            "rom_path": normalized_rom_path,
            "created": created,
            "updated": updated,
            "removed": removed,
            "title": _text_or_empty(game, "name") or Path(normalized_rom_path).stem,
            "search_title": _clean_rom_title(_text_or_empty(game, "name") or Path(normalized_rom_path).stem),
            "missing": self._entry_missing_artwork(game),
            "existing": {field: _text_or_empty(game, field) for field in ARTWORK_FIELDS},
            "gamelist": _gamelist_details(game),
            "has_gamelist_entry": True,
        }

    def find_rom_by_unique_id(self, system: str, unique_id: str) -> dict:
        _, roms = self.list_assets(system, "roms")
        for rom in roms:
            if str(rom.get("unique_id")) == str(unique_id):
                return rom
        raise FileNotFoundError()

    def find_rom_by_path(self, system: str, rom_path: str) -> dict:
        system_dir = self.get_system_dir(system)
        normalized_path = _normalize_gamelist_rom_path(rom_path)
        if not normalized_path or "\x00" in normalized_path:
            raise ValueError("invalid rom_path")
        target_path = (system_dir / normalized_path).resolve()
        if target_path != system_dir and system_dir not in target_path.parents:
            raise ValueError("rom_path is outside system directory")
        if not target_path.exists():
            raise FileNotFoundError()
        name = target_path.stem if target_path.is_file() else target_path.name
        return {
            "unique_id": "",
            "name": name,
            "rom_file": target_path.name,
            "rom_path": normalized_path,
            "image_stem": name,
            "entry_type": "folder" if target_path.is_dir() else "file",
            "is_downloadable": target_path.is_file(),
        }
