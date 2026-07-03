"""RomRepository scraper-artwork-apply methods, as a mixin.

Extracted from ``drone_api.py``. These apply scraped artwork (LaunchBox / TheGamesDB /
MobyGames / a generic remote payload) onto the gamelist + filesystem. Kept as a mixin
composed onto ``RomRepository`` so the methods stay ``self``-bound (they use the
repository's gamelist/find/cache helpers) with call sites unchanged.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

try:
    from ..common.runtime_state import _GAMELIST_WRITE_LOCK
    from .gamelist import (
        ARTWORK_FIELDS,
        _artwork_identity,
        _gamelist_details,
        _relative_artwork_path,
        _set_child_text,
        _text_or_empty,
    )
    from .scrapers import LaunchBoxClient, MobyGamesClient, TheGamesDBScraper, _clean_rom_title
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.runtime_state import _GAMELIST_WRITE_LOCK  # type: ignore
    from roms.gamelist import (  # type: ignore
        ARTWORK_FIELDS,
        _artwork_identity,
        _gamelist_details,
        _relative_artwork_path,
        _set_child_text,
        _text_or_empty,
    )
    from roms.scrapers import LaunchBoxClient, MobyGamesClient, TheGamesDBScraper, _clean_rom_title  # type: ignore


class RomArtworkApplyMixin:
    def apply_remote_artwork(
        self,
        system: str,
        unique_id: str,
        rom_path: Optional[str],
        field: str,
        image_data: bytes,
        content_type: str,
        source_url: str,
        source_label: str = "remote",
    ) -> dict:
        if field not in ARTWORK_FIELDS:
            raise ValueError("invalid artwork field")
        system_dir = self.get_system_dir(system)
        rom = self.find_rom_by_path(system, rom_path) if rom_path else self.find_rom_by_unique_id(system, unique_id)
        # Serialize the gamelist read-modify-write (and the image write keyed off it)
        # against the artwork-download worker and other gamelist writers.
        with _GAMELIST_WRITE_LOCK:
            tree, root = self._read_gamelist(system_dir)
            rom_name = str(rom.get("rom_file") or rom.get("rom_name") or rom.get("name") or "")
            normalized_rom_path = str(rom.get("rom_path") or rom_path or rom_name)
            display_name = str(rom.get("image_stem") or rom.get("name") or Path(normalized_rom_path).stem)
            game = self._find_gamelist_entry_by_path(root, normalized_rom_path) or self._find_gamelist_entry(root, rom_name, display_name)
            if game is None:
                game = ET.SubElement(root, "game")
                _set_child_text(game, "path", f"./{normalized_rom_path}")
                _set_child_text(game, "name", _clean_rom_title(display_name))

            parsed_suffix = Path(urlparse(source_url).path).suffix.lower()
            if parsed_suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                parsed_suffix = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                }.get(content_type, ".jpg")
            images_dir = system_dir / "images"
            images_dir.mkdir(parents=True, exist_ok=True)
            safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(display_name).stem).strip("-") or "rom"
            safe_label = re.sub(r"[^a-zA-Z0-9._-]+", "-", source_label).strip("-") or "remote"
            target_path = images_dir / f"{safe_stem}-{safe_label}-{field}{parsed_suffix}"
            target_path.write_bytes(image_data)
            relative_path = _relative_artwork_path(system_dir, target_path)
            _set_child_text(game, field, relative_path)

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
            "unique_id": unique_id,
            "rom_name": rom_name or display_name,
            "rom_path": normalized_rom_path,
            "updated": [{"field": field, "path": relative_path, "source_url": source_url}],
            "missing": self._entry_missing_artwork(game),
            "existing": {item: _text_or_empty(game, item) for item in ARTWORK_FIELDS},
            "gamelist": _gamelist_details(game),
            "has_gamelist_entry": True,
        }

    def apply_launchbox_artwork(
        self,
        system: str,
        unique_id: str,
        game_key: str,
        client: LaunchBoxClient,
        rom_path: Optional[str] = None,
        override_existing: bool = False,
        import_metadata: bool = False,
    ) -> dict:
        system_dir = self.get_system_dir(system)
        rom = self.find_rom_by_path(system, rom_path) if rom_path else self.find_rom_by_unique_id(system, unique_id)
        tree, root = self._read_gamelist(system_dir)
        rom_name = str(rom.get("rom_file") or rom.get("rom_name") or rom.get("name") or "")
        normalized_rom_path = str(rom.get("rom_path") or rom_name)
        display_name = str(rom.get("image_stem") or rom.get("name") or "")
        game = self._find_gamelist_entry_by_path(root, normalized_rom_path) or self._find_gamelist_entry(root, rom_name, display_name)
        if game is None:
            game = ET.SubElement(root, "game")
            _set_child_text(game, "path", f"./{normalized_rom_path}")
            _set_child_text(game, "name", _clean_rom_title(display_name))

        if override_existing:
            fields_to_fetch = list(ARTWORK_FIELDS)
        else:
            fields_to_fetch = self._entry_missing_artwork(game)
        if not fields_to_fetch and not import_metadata:
            return {"system": system, "unique_id": unique_id, "updated": [], "skipped": list(ARTWORK_FIELDS)}

        details = client.details(game_key)
        images_dir = system_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(display_name).stem).strip("-") or "rom"
        updated = []
        skipped = []
        used_source_urls = {
            _artwork_identity(_text_or_empty(game, field))
            for field in ARTWORK_FIELDS
            if _text_or_empty(game, field)
        }

        # Import metadata if requested
        if import_metadata and details:
            meta_fields_map = {
                "name": details.get("name"),
                "desc": details.get("overview"),
                "releasedate": details.get("release_date"),
                "genre": details.get("genre"),
                "developer": details.get("developer"),
                "publisher": details.get("publisher"),
                "players": details.get("players"),
                "rating": details.get("rating"),
            }
            for mfield, mvalue in meta_fields_map.items():
                if mvalue and (override_existing or not _text_or_empty(game, mfield)):
                    _set_child_text(game, mfield, str(mvalue))
                    updated.append({"field": mfield, "value": str(mvalue), "source": "launchbox_metadata"})

        for field in fields_to_fetch:
            selected = client.choose_image_for_field(details, field)
            if not selected:
                skipped.append(field)
                continue
            source_url = str(selected.get("url") or "")
            source_identity = _artwork_identity(source_url)
            if source_identity and source_identity in used_source_urls:
                skipped.append(field)
                continue
            try:
                data, content_type = client.download_image(source_url)
            except Exception:
                skipped.append(field)
                continue
            source_suffix = Path(str(selected.get("file_name") or "")).suffix.lower()
            if source_suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                source_suffix = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                }.get(content_type, ".jpg")
            target_path = images_dir / f"{safe_stem}-launchbox-{field}{source_suffix}"
            target_path.write_bytes(data)
            relative_path = _relative_artwork_path(system_dir, target_path)
            _set_child_text(game, field, relative_path)
            used_source_urls.update({source_identity, _artwork_identity(relative_path)})
            updated.append(
                {
                    "field": field,
                    "path": relative_path,
                    "source_url": source_url,
                    "type": selected.get("type"),
                    "region": selected.get("region"),
                }
            )

        if updated:
            # Persist under the gamelist lock with a fresh read-merge-write: the
            # image fetches above ran without the lock (network I/O), so re-read the
            # current gamelist and re-apply just the fields we changed, rather than
            # writing a stale tree that could clobber a concurrent writer.
            with _GAMELIST_WRITE_LOCK:
                tree, root = self._read_gamelist(system_dir)
                game = self._find_gamelist_entry_by_path(root, normalized_rom_path) or self._find_gamelist_entry(root, rom_name, display_name)
                if game is None:
                    game = ET.SubElement(root, "game")
                    _set_child_text(game, "path", f"./{normalized_rom_path}")
                    _set_child_text(game, "name", _clean_rom_title(display_name))
                for item in updated:
                    value = item.get("path") or item.get("value")
                    if item.get("field") and value:
                        _set_child_text(game, str(item["field"]), str(value))
                gamelist_path = system_dir / "gamelist.xml"
                try:
                    ET.indent(tree, space="  ")
                except Exception:
                    pass
                tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
                with gamelist_path.open("a", encoding="utf-8") as handle:
                    handle.write("\n")

        return {
            "system": system,
            "unique_id": unique_id,
            "rom_name": rom_name,
            "rom_path": normalized_rom_path,
            "launchbox": {
                "game_key": details.get("game_key"),
                "name": details.get("name"),
                "platform": details.get("platform"),
            },
            "updated": updated,
            "skipped": skipped,
            "missing": self._entry_missing_artwork(game),
            "existing": {item: _text_or_empty(game, item) for item in ARTWORK_FIELDS},
            "gamelist": _gamelist_details(game),
            "has_gamelist_entry": True,
        }

    def apply_thegamesdb_artwork(
        self,
        system: str,
        unique_id: str,
        game_id: str,
        scraper: TheGamesDBScraper,
        rom_path: Optional[str] = None,
        override_existing: bool = False,
        import_metadata: bool = True,
    ) -> dict:
        system_dir = self.get_system_dir(system)
        rom = self.find_rom_by_path(system, rom_path) if rom_path else self.find_rom_by_unique_id(system, unique_id)
        tree, root = self._read_gamelist(system_dir)
        rom_name = str(rom.get("rom_file") or rom.get("rom_name") or rom.get("name") or "")
        normalized_rom_path = str(rom.get("rom_path") or rom_path or rom_name)
        display_name = str(rom.get("image_stem") or rom.get("name") or Path(normalized_rom_path).stem)
        game = self._find_gamelist_entry_by_path(root, normalized_rom_path) or self._find_gamelist_entry(root, rom_name, display_name)
        if game is None:
            game = ET.SubElement(root, "game")
            _set_child_text(game, "path", f"./{normalized_rom_path}")
            _set_child_text(game, "name", _clean_rom_title(display_name))

        fields_to_fetch = list(ARTWORK_FIELDS) if override_existing else self._entry_missing_artwork(game)
        details = scraper.details(game_id)
        images_dir = system_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(display_name).stem).strip("-") or "rom"
        updated = []
        skipped = []

        if import_metadata and details:
            meta_fields_map = {
                "name": details.get("name"),
                "desc": details.get("overview"),
                "releasedate": details.get("release_date"),
                "genre": details.get("genre"),
                "developer": details.get("developer"),
                "publisher": details.get("publisher"),
                "players": details.get("players"),
                "rating": details.get("rating"),
            }
            for mfield, mvalue in meta_fields_map.items():
                if not mvalue:
                    continue
                existing_value = _text_or_empty(game, mfield)
                if override_existing or not existing_value:
                    _set_child_text(game, mfield, str(mvalue))
                    updated.append({"field": mfield, "value": str(mvalue), "source": "thegamesdb_metadata"})

        for field in fields_to_fetch:
            selected = scraper.choose_image_for_field(details, field)
            if not selected:
                skipped.append(field)
                continue
            source_url = str(selected.get("url") or selected.get("image_url") or "")
            try:
                data, content_type = scraper.download_image(source_url)
            except Exception:
                skipped.append(field)
                continue
            source_suffix = Path(str(selected.get("file_name") or urlparse(source_url).path)).suffix.lower()
            if source_suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                source_suffix = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                }.get(content_type, ".jpg")
            target_path = images_dir / f"{safe_stem}-thegamesdb-{field}{source_suffix}"
            target_path.write_bytes(data)
            relative_path = _relative_artwork_path(system_dir, target_path)
            _set_child_text(game, field, relative_path)
            updated.append(
                {
                    "field": field,
                    "path": relative_path,
                    "source_url": source_url,
                    "type": selected.get("type"),
                }
            )

        if updated:
            # Re-read-merge-write under the gamelist lock: image fetches above ran
            # without the lock, so persist against the current on-disk gamelist
            # instead of a stale tree that could clobber a concurrent writer.
            with _GAMELIST_WRITE_LOCK:
                tree, root = self._read_gamelist(system_dir)
                game = self._find_gamelist_entry_by_path(root, normalized_rom_path) or self._find_gamelist_entry(root, rom_name, display_name)
                if game is None:
                    game = ET.SubElement(root, "game")
                    _set_child_text(game, "path", f"./{normalized_rom_path}")
                    _set_child_text(game, "name", _clean_rom_title(display_name))
                for item in updated:
                    value = item.get("path") or item.get("value")
                    if item.get("field") and value:
                        _set_child_text(game, str(item["field"]), str(value))
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
            "unique_id": unique_id,
            "rom_name": rom_name or display_name,
            "rom_path": normalized_rom_path,
            "thegamesdb": {
                "game_id": details.get("game_id"),
                "name": details.get("name"),
            },
            "updated": updated,
            "skipped": skipped,
            "missing": self._entry_missing_artwork(game),
            "existing": {item: _text_or_empty(game, item) for item in ARTWORK_FIELDS},
            "gamelist": _gamelist_details(game),
            "has_gamelist_entry": True,
        }

    def apply_mobygames_artwork(
        self,
        system: str,
        unique_id: str,
        game_id: str,
        client: MobyGamesClient,
        rom_path: Optional[str] = None,
        override_existing: bool = False,
        import_metadata: bool = True,
    ) -> dict:
        system_dir = self.get_system_dir(system)
        rom = self.find_rom_by_path(system, rom_path) if rom_path else self.find_rom_by_unique_id(system, unique_id)
        tree, root = self._read_gamelist(system_dir)
        rom_name = str(rom.get("rom_file") or rom.get("rom_name") or rom.get("name") or "")
        normalized_rom_path = str(rom.get("rom_path") or rom_path or rom_name)
        display_name = str(rom.get("image_stem") or rom.get("name") or Path(normalized_rom_path).stem)
        game = self._find_gamelist_entry_by_path(root, normalized_rom_path) or self._find_gamelist_entry(root, rom_name, display_name)
        if game is None:
            game = ET.SubElement(root, "game")
            _set_child_text(game, "path", f"./{normalized_rom_path}")
            _set_child_text(game, "name", _clean_rom_title(display_name))

        fields_to_fetch = list(ARTWORK_FIELDS) if override_existing else self._entry_missing_artwork(game)
        details = client.details(game_id, system=system)
        images_dir = system_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(display_name).stem).strip("-") or "rom"
        updated = []
        skipped = []

        if import_metadata and details:
            meta_fields_map = {
                "name": details.get("name"),
                "desc": details.get("overview"),
                "releasedate": details.get("release_date"),
                "genre": details.get("genre"),
                "developer": details.get("developer"),
                "publisher": details.get("publisher"),
            }
            for mfield, mvalue in meta_fields_map.items():
                if not mvalue:
                    continue
                existing_value = _text_or_empty(game, mfield)
                if override_existing or not existing_value:
                    _set_child_text(game, mfield, str(mvalue))
                    updated.append({"field": mfield, "value": str(mvalue), "source": "mobygames_metadata"})

        for field in fields_to_fetch:
            selected = client.choose_image_for_field(details, field)
            if not selected:
                skipped.append(field)
                continue
            source_url = str(selected.get("url") or selected.get("image_url") or "")
            try:
                data, content_type = client.download_image(source_url)
            except Exception:
                skipped.append(field)
                continue
            source_suffix = Path(str(selected.get("file_name") or urlparse(source_url).path)).suffix.lower()
            if source_suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                source_suffix = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                }.get(content_type, ".jpg")
            target_path = images_dir / f"{safe_stem}-mobygames-{field}{source_suffix}"
            target_path.write_bytes(data)
            relative_path = _relative_artwork_path(system_dir, target_path)
            _set_child_text(game, field, relative_path)
            updated.append(
                {
                    "field": field,
                    "path": relative_path,
                    "source_url": source_url,
                    "type": selected.get("type"),
                }
            )

        if updated:
            # Re-read-merge-write under the gamelist lock: image fetches above ran
            # without the lock, so persist against the current on-disk gamelist
            # instead of a stale tree that could clobber a concurrent writer.
            with _GAMELIST_WRITE_LOCK:
                tree, root = self._read_gamelist(system_dir)
                game = self._find_gamelist_entry_by_path(root, normalized_rom_path) or self._find_gamelist_entry(root, rom_name, display_name)
                if game is None:
                    game = ET.SubElement(root, "game")
                    _set_child_text(game, "path", f"./{normalized_rom_path}")
                    _set_child_text(game, "name", _clean_rom_title(display_name))
                for item in updated:
                    value = item.get("path") or item.get("value")
                    if item.get("field") and value:
                        _set_child_text(game, str(item["field"]), str(value))
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
            "unique_id": unique_id,
            "rom_name": rom_name or display_name,
            "rom_path": normalized_rom_path,
            "mobygames": {
                "game_id": details.get("game_id"),
                "name": details.get("name"),
            },
            "updated": updated,
            "skipped": skipped,
            "missing": self._entry_missing_artwork(game),
            "existing": {item: _text_or_empty(game, item) for item in ARTWORK_FIELDS},
            "gamelist": _gamelist_details(game),
            "has_gamelist_entry": True,
        }
