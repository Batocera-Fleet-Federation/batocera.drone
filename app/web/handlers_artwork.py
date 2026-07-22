"""RomRequestHandler admin artwork/scraper handlers, as a mixin.

Extracted from ``drone_api.py``. Missing-artwork listing, LaunchBox / TheGamesDB /
MobyGames artwork search+apply, artwork upload, and gamelist entry remove/update/
remove-missing. Composed onto ``RomRequestHandler`` (methods stay ``self``-bound; ROM
lookups go through ``self.repository``).
"""

import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote, unquote

try:
    from ..common.http_cache import valid_segment
    from ..common.runtime_state import _GAMELIST_WRITE_LOCK
    from .route_config import api_url
    from ..roms.gamelist import (
        ARTWORK_DUPLICATE_FILTER,
        ARTWORK_FIELDS,
        _gamelist_details,
        _normalize_gamelist_rom_path,
        _relative_artwork_path,
        _set_child_text,
        _text_or_empty,
    )
    from ..roms.scrapers import (
        LaunchBoxClient,
        MobyGamesClient,
        ScraperUnavailableError,
        TheGamesDBScraper,
        _clean_rom_title,
        _launchbox_platform_for_system,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.http_cache import valid_segment  # type: ignore
    from common.runtime_state import _GAMELIST_WRITE_LOCK  # type: ignore
    from web.route_config import api_url  # type: ignore
    from roms.gamelist import (  # type: ignore
        ARTWORK_DUPLICATE_FILTER,
        ARTWORK_FIELDS,
        _gamelist_details,
        _normalize_gamelist_rom_path,
        _relative_artwork_path,
        _set_child_text,
        _text_or_empty,
    )
    from roms.scrapers import (  # type: ignore
        LaunchBoxClient,
        MobyGamesClient,
        ScraperUnavailableError,
        TheGamesDBScraper,
        _clean_rom_title,
        _launchbox_platform_for_system,
    )


class HandlersArtworkMixin:
    def _handle_admin_artwork_missing(
        self,
        include_filesystem: bool = False,
        refresh: bool = False,
        limit: int = 200,
        offset: int = 0,
        art_fields: Optional[List[str]] = None,
        system_filters: Optional[List[str]] = None,
        query: Optional[str] = None,
        rom_status: Optional[str] = None,
    ) -> None:
        started_at = time.time()
        normalized_art_fields = {
            str(field or "").strip().lower()
            for field in (art_fields or [])
            if str(field or "").strip()
        }
        include_complete = "show_all" in normalized_art_fields
        items = self.repository.list_missing_artwork(
            include_filesystem=include_filesystem,
            force_refresh=refresh,
            include_complete=include_complete,
        )
        systems_all = sorted({str(item.get("system") or "") for item in items if item.get("system")})
        if "any" in normalized_art_fields or include_complete:
            normalized_art_fields = set()
        valid_art_filters = set(ARTWORK_FIELDS) | {ARTWORK_DUPLICATE_FILTER}
        normalized_art_fields = {field for field in normalized_art_fields if field in valid_art_filters}
        normalized_systems = {
            str(system or "").strip().lower()
            for system in (system_filters or [])
            if str(system or "").strip()
        }
        normalized_query = str(query or "").strip().lower()
        normalized_rom_status = str(rom_status or "any").strip().lower()
        if normalized_rom_status not in ("any", "exists", "missing"):
            normalized_rom_status = "any"

        filtered_items = items
        items_with_status = []
        for item in filtered_items:
            next_item = dict(item)
            next_item["rom_exists"] = self.repository._rom_path_exists(
                str(next_item.get("system") or ""),
                str(next_item.get("rom_path") or next_item.get("rom_name") or ""),
            )
            items_with_status.append(next_item)
        filtered_items = items_with_status
        if normalized_art_fields:
            filtered_items = [
                item
                for item in filtered_items
                if normalized_art_fields.intersection({str(field).lower() for field in (item.get("missing") or [])})
            ]
        if normalized_systems:
            filtered_items = [
                item
                for item in filtered_items
                if str(item.get("system") or "").strip().lower() in normalized_systems
            ]
        if normalized_query:
            filtered_items = [
                item
                for item in filtered_items
                if normalized_query
                in " ".join(
                    [
                        str(item.get("system") or ""),
                        str(item.get("name") or ""),
                        str(item.get("title") or ""),
                        str(item.get("rom_name") or ""),
                        str(item.get("rom_path") or ""),
                        " ".join(str(field) for field in (item.get("missing") or [])),
                    ]
                ).lower()
            ]
        if normalized_rom_status == "exists":
            filtered_items = [item for item in filtered_items if bool(item.get("rom_exists"))]
        elif normalized_rom_status == "missing":
            filtered_items = [item for item in filtered_items if not bool(item.get("rom_exists"))]

        total = len(filtered_items)
        safe_limit = max(1, min(int(limit), 500))
        safe_offset = max(0, int(offset))
        page_items = [dict(item) for item in filtered_items[safe_offset : safe_offset + safe_limit]]
        systems_filtered = sorted({str(item.get("system") or "") for item in filtered_items if item.get("system")})
        field_counts = {field: 0 for field in (*ARTWORK_FIELDS, ARTWORK_DUPLICATE_FILTER)}
        for item in filtered_items:
            for field in item.get("missing") or []:
                if field in field_counts:
                    field_counts[field] += 1
        self._send_json(
            200,
            {
                "roms": page_items,
                "count": total,
                "returned": len(page_items),
                "limit": safe_limit,
                "offset": safe_offset,
                "has_more": (safe_offset + len(page_items)) < total,
                "systems": systems_all,
                "systems_filtered": systems_filtered,
                "fields": list(ARTWORK_FIELDS) + [ARTWORK_DUPLICATE_FILTER],
                "field_counts": field_counts,
                "selected_fields": ["show_all"] if include_complete else (sorted(normalized_art_fields) if normalized_art_fields else ["any"]),
                "selected_systems": sorted(normalized_systems),
                "rom_status": normalized_rom_status,
                "query": normalized_query,
                "mode": "filesystem" if include_filesystem else "gamelist",
                "show_all": include_complete,
                "cached": not refresh,
                "elapsed_ms": int((time.time() - started_at) * 1000),
            },
        )

    def _handle_admin_launchbox_search(self, system: str, rom_id: str, rom_path: str, query: str) -> None:
        system_value = (system or "").strip()
        rom_id_value = (rom_id or "").strip()
        rom_path_value = _normalize_gamelist_rom_path(rom_path)
        query_value = (query or "").strip()
        if system_value and not query_value and (rom_path_value or rom_id_value):
            rom = self.repository.find_rom_by_path(system_value, rom_path_value) if rom_path_value else self.repository.find_rom_by_unique_id(system_value, rom_id_value)
            query_value = _clean_rom_title(str(rom.get("image_stem") or rom.get("name") or ""))
        elif query_value:
            query_value = _clean_rom_title(query_value)
        if not query_value:
            raise ValueError("q or system+rom_id/rom_path is required")
        client = LaunchBoxClient()
        launchbox_unavailable = False
        launchbox_error = ""
        try:
            matches = client.search(query_value, system=system_value or None)
        except ScraperUnavailableError as error:
            matches = []
            launchbox_unavailable = True
            launchbox_error = str(error)
        self._send_json(
            200,
            {
                "query": query_value,
                "system": system_value,
                "launchbox_platform": _launchbox_platform_for_system(system_value),
                "launchbox_unavailable": launchbox_unavailable,
                "launchbox_error": launchbox_error,
                "rom_id": rom_id_value,
                "rom_path": rom_path_value,
                "matches": matches,
            },
        )

    def _handle_admin_launchbox_apply(self, payload: dict) -> None:
        system = str(payload.get("system") or "").strip()
        rom_id = str(payload.get("rom_id") or payload.get("unique_id") or "").strip()
        rom_path = _normalize_gamelist_rom_path(str(payload.get("rom_path") or ""))
        game_key = str(payload.get("game_key") or "").strip()
        override_existing = bool(payload.get("override_existing", False))
        import_metadata = bool(payload.get("import_metadata", False))
        if not system:
            raise ValueError("system is required")
        if not rom_id and not rom_path:
            raise ValueError("rom_id or rom_path is required")
        if not game_key:
            raise ValueError("game_key is required")
        client = LaunchBoxClient()
        result = self.repository.apply_launchbox_artwork(
            system, rom_id, game_key, client, rom_path=rom_path or None,
            override_existing=override_existing, import_metadata=import_metadata
        )
        with self.repository._search_cache_lock:
            self.repository._search_index_expires_at = 0
        with self.repository._missing_artwork_cache_lock:
            self.repository._missing_artwork_cache.clear()
        result["override_existing"] = override_existing
        result["metadata_imported"] = len([item for item in result.get("updated", []) if str(item.get("source") or "") == "launchbox_metadata"])
        self._send_json(200, result)

    def _handle_admin_thegamesdb_artwork_search(self, system: str, rom_id: str, rom_path: str, query: str) -> None:
        system_value = (system or "").strip()
        rom_id_value = (rom_id or "").strip()
        rom_path_value = _normalize_gamelist_rom_path(rom_path)
        query_value = (query or "").strip()
        title_value = query_value
        if system_value and not title_value and (rom_path_value or rom_id_value):
            rom = self.repository.find_rom_by_path(system_value, rom_path_value) if rom_path_value else self.repository.find_rom_by_unique_id(system_value, rom_id_value)
            title_value = str(rom.get("image_stem") or rom.get("name") or "")
        title_value = _clean_rom_title(title_value)
        if not title_value:
            raise ValueError("q or system+rom_id/rom_path is required")
        scraper = TheGamesDBScraper()
        matches = scraper.search(title_value, system=system_value, limit=5)
        self._send_json(
            200,
            {
                "query": title_value,
                "system": system_value,
                "rom_id": rom_id_value,
                "rom_path": rom_path_value,
                "matches": matches,
                "fields": list(ARTWORK_FIELDS),
            },
        )

    def _handle_admin_thegamesdb_artwork_apply(self, payload: dict) -> None:
        system = str(payload.get("system") or "").strip()
        rom_id = str(payload.get("rom_id") or payload.get("unique_id") or "").strip()
        rom_path = _normalize_gamelist_rom_path(str(payload.get("rom_path") or ""))
        game_id = str(payload.get("game_id") or "").strip()
        override_existing = bool(payload.get("override_existing", False))
        import_metadata = bool(payload.get("import_metadata", True))
        if not system:
            raise ValueError("system is required")
        if not rom_id and not rom_path:
            raise ValueError("rom_id or rom_path is required")
        if not game_id:
            raise ValueError("game_id is required")
        scraper = TheGamesDBScraper()
        result = self.repository.apply_thegamesdb_artwork(
            system,
            rom_id,
            game_id,
            scraper,
            rom_path=rom_path or None,
            override_existing=override_existing,
            import_metadata=import_metadata,
        )
        with self.repository._search_cache_lock:
            self.repository._search_index_expires_at = 0
        result["source"] = "thegamesdb"
        result["override_existing"] = override_existing
        result["metadata_imported"] = len([item for item in result.get("updated", []) if str(item.get("source") or "") == "thegamesdb_metadata"])
        self._send_json(200, result)

    def _handle_admin_mobygames_artwork_search(self, system: str, rom_id: str, rom_path: str, query: str) -> None:
        system_value = (system or "").strip()
        rom_id_value = (rom_id or "").strip()
        rom_path_value = _normalize_gamelist_rom_path(rom_path)
        query_value = (query or "").strip()
        title_value = query_value
        if system_value and not title_value and (rom_path_value or rom_id_value):
            rom = self.repository.find_rom_by_path(system_value, rom_path_value) if rom_path_value else self.repository.find_rom_by_unique_id(system_value, rom_id_value)
            title_value = str(rom.get("image_stem") or rom.get("name") or "")
        title_value = _clean_rom_title(title_value)
        if not title_value:
            raise ValueError("q or system+rom_id/rom_path is required")
        self._send_json(
            200,
            {
                "query": title_value,
                "system": system_value,
                "mobygames_platform": MobyGamesClient().platform_name_for_system(system_value),
                "rom_id": rom_id_value,
                "rom_path": rom_path_value,
                "matches": [],
                "configured": False,
                "message": "MobyGames scraping is disabled because the site often requires a browser challenge. Use the MobyGames link to search manually.",
                "fields": list(ARTWORK_FIELDS),
            },
        )

    def _handle_admin_mobygames_artwork_apply(self, payload: dict) -> None:
        system = str(payload.get("system") or "").strip()
        rom_id = str(payload.get("rom_id") or payload.get("unique_id") or "").strip()
        rom_path = _normalize_gamelist_rom_path(str(payload.get("rom_path") or ""))
        game_id = str(payload.get("game_id") or "").strip()
        if not system:
            raise ValueError("system is required")
        if not rom_id and not rom_path:
            raise ValueError("rom_id or rom_path is required")
        if not game_id:
            raise ValueError("game_id is required")
        raise ValueError("MobyGames scraping is disabled because the site often requires a browser challenge. Use the MobyGames link to search manually.")

    def _handle_admin_artwork_upload(self) -> None:
        import urllib.parse
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("multipart/form-data expected")
        # Read raw multipart body
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0 or content_length > 50 * 1024 * 1024:
            raise ValueError("invalid content size")
        raw_body = self.rfile.read(content_length)
        # Parse using simple field extraction for file upload
        boundary = content_type.split("boundary=")[1].strip() if "boundary=" in content_type else None
        if not boundary:
            raise ValueError("boundary not found in content-type")
        boundary = boundary.strip('"').strip("'")
        # Simple multipart parser for file + fields
        parts = raw_body.split(f"--{boundary}".encode())
        field_name = None
        system = None
        rom_id = None
        rom_path = None
        file_data = None
        filename = None
        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            lines = part.split(b"\r\n")
            disposition = b""
            for line in lines:
                if b"Content-Disposition" in line:
                    disposition = line
                    break
            disp_str = disposition.decode("utf-8", errors="replace")
            # Determine field name
            fname = None
            if ' name="' in disp_str:
                fname = disp_str.split(' name="')[1].split('"')[0]
            # Check for filename
            has_file = ' filename="' in disp_str
            fn = None
            if has_file:
                fn = disp_str.split(' filename="')[1].split('"')[0]
            # Find payload (after headers)
            header_end = part.find(b"\r\n\r\n")
            payload_start = header_end + 4 if header_end >= 0 else 0
            payload = lines[-1:] if len(lines) == 1 else raw_body  # simplified
            # Re-extract payload properly
            payload = part[part.find(b"\r\n\r\n")+4:] if b"\r\n\r\n" in part else b""
            payload = payload.rstrip(b"\r\n").rstrip(b"--")
            if has_file and fn:
                file_data = payload
                filename = fn
            elif fname:
                value = payload.decode("utf-8", errors="replace").strip()
                if fname == "field":
                    field_name = value
                elif fname == "system":
                    system = value
                elif fname == "rom_id":
                    rom_id = value
                elif fname == "rom_path":
                    rom_path = _normalize_gamelist_rom_path(value)
        if not file_data or not field_name or not system or (not rom_id and not rom_path):
            raise ValueError("file, field, system, and rom_id or rom_path are required")
        if field_name not in ARTWORK_FIELDS:
            raise ValueError("invalid artwork field")
        filename = filename or f"{field_name}.png"
        # Find the ROM to update its gamelist and images
        system_dir = self.repository.get_system_dir(system)
        # Try to find the ROM by unique_id first, then by path
        try:
            rom = self.repository.find_rom_by_unique_id(system, rom_id) if rom_id else self.repository.find_rom_by_path(system, rom_path or "")
        except FileNotFoundError:
            try:
                rom = self.repository.find_rom_by_path(system, rom_path or rom_id)
            except FileNotFoundError:
                # Just use rom_id as a name stem if not found
                fallback = rom_path or rom_id
                rom = {"name": Path(fallback).stem or fallback, "image_stem": Path(fallback).stem or fallback, "rom_path": rom_path or fallback}
        images_dir = (system_dir / "images").resolve()
        images_dir.mkdir(parents=True, exist_ok=True)
        display_name = str(rom.get("image_stem") or rom.get("name") or rom_id)
        safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(display_name).stem).strip("-") or "rom"
        dest_filename = f"{safe_stem}-{field_name}{Path(filename).suffix}"
        dest_path = images_dir / dest_filename
        with open(dest_path, "wb") as f:
            f.write(file_data)
        relative_path = _relative_artwork_path(system_dir, dest_path)
        normalized_rom_path = _normalize_gamelist_rom_path(rom_path or str(rom.get("rom_path") or ""))
        game = None
        gamelist_details = {}
        existing = {field: "" for field in ARTWORK_FIELDS}
        missing = list(ARTWORK_FIELDS)
        has_gamelist_entry = False
        # Update gamelist if possible. Serialize the read-modify-write against the
        # artwork-download worker and other gamelist writers.
        try:
            with _GAMELIST_WRITE_LOCK:
                tree, root = self.repository._read_gamelist(system_dir)
                game = self.repository._find_gamelist_entry_by_path(root, normalized_rom_path)
                if game is None:
                    rom_name = str(rom.get("rom_file") or Path(normalized_rom_path or rom_id).name)
                    display_name = str(rom.get("image_stem") or rom.get("name") or Path(rom_name).stem)
                    game = self.repository._find_gamelist_entry(root, rom_name, display_name)
                if game is None and normalized_rom_path:
                    display_name = str(rom.get("image_stem") or rom.get("name") or Path(normalized_rom_path).stem)
                    game = ET.SubElement(root, "game")
                    _set_child_text(game, "path", f"./{normalized_rom_path}")
                    _set_child_text(game, "name", _clean_rom_title(display_name))
                if game is not None:
                    _set_child_text(game, field_name, relative_path)
                    gamelist_path = system_dir / "gamelist.xml"
                    try:
                        ET.indent(tree, space="  ")
                    except Exception:
                        pass
                    tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
                    with gamelist_path.open("a", encoding="utf-8") as handle:
                        handle.write("\n")
                    gamelist_details = _gamelist_details(game)
                    existing = {field: _text_or_empty(game, field) for field in ARTWORK_FIELDS}
                    missing = self.repository._entry_missing_artwork(game)
                    has_gamelist_entry = True
        except Exception:
            pass  # Gamelist write is best-effort for manual uploads
        # Invalidate caches
        with self.repository._missing_artwork_cache_lock:
            self.repository._missing_artwork_cache.clear()
        rom_name = str(rom.get("name") or Path(rom_path or rom_id).stem or rom_path or rom_id)
        self._send_json(200, {
            "rom_name": rom_name,
            "field": field_name,
            "path": str(dest_path),
            "relative_path": relative_path,
            "url": api_url(f"/public/systems/{quote(system, safe='')}/images/{quote(dest_filename, safe='')}"),
            "existing": existing,
            "missing": missing,
            "gamelist": gamelist_details,
            "has_gamelist_entry": has_gamelist_entry,
        })

    def _handle_admin_gamelist_remove(self, payload: dict) -> None:
        system = str(payload.get("system") or "").strip()
        rom_path = _normalize_gamelist_rom_path(str(payload.get("rom_path") or ""))
        if not system:
            raise ValueError("system is required")
        if not rom_path:
            raise ValueError("rom_path is required")
        result = self.repository.remove_gamelist_entry(system, rom_path)
        self._send_json(200, result)

    def _handle_admin_gamelist_update(self, payload: dict) -> None:
        system = str(payload.get("system") or "").strip()
        rom_path = _normalize_gamelist_rom_path(str(payload.get("rom_path") or ""))
        fields = payload.get("fields")
        if not system:
            raise ValueError("system is required")
        if not rom_path:
            raise ValueError("rom_path is required")
        if not isinstance(fields, dict):
            raise ValueError("fields must be an object")
        result = self.repository.update_gamelist_entry(system, rom_path, fields)
        self._send_json(200, result)

    def _handle_admin_gamelist_remove_missing(self, payload: dict) -> None:
        confirm = str(payload.get("confirm") or "").strip()
        if confirm != "DELETE_MISSING_GAMELIST_ENTRIES":
            raise ValueError("confirm must be DELETE_MISSING_GAMELIST_ENTRIES")
        include_filesystem = bool(payload.get("include_filesystem"))
        art_fields = payload.get("fields") if isinstance(payload.get("fields"), list) else []
        system_filters = payload.get("systems") if isinstance(payload.get("systems"), list) else []
        query = str(payload.get("q") or "")

        items = self.repository.list_missing_artwork(include_filesystem=include_filesystem, force_refresh=False)
        normalized_art_fields = {str(field or "").strip().lower() for field in art_fields if str(field or "").strip()}
        if "any" in normalized_art_fields:
            normalized_art_fields = set()
        normalized_art_fields = {field for field in normalized_art_fields if field in ARTWORK_FIELDS}
        normalized_systems = {str(system or "").strip().lower() for system in system_filters if str(system or "").strip()}
        normalized_query = query.strip().lower()

        filtered = []
        for item in items:
            candidate = dict(item)
            candidate["rom_exists"] = self.repository._rom_path_exists(
                str(candidate.get("system") or ""),
                str(candidate.get("rom_path") or candidate.get("rom_name") or ""),
            )
            if candidate["rom_exists"]:
                continue
            if normalized_art_fields and not normalized_art_fields.intersection({str(field).lower() for field in (candidate.get("missing") or [])}):
                continue
            if normalized_systems and str(candidate.get("system") or "").strip().lower() not in normalized_systems:
                continue
            if normalized_query:
                haystack = " ".join(
                    [
                        str(candidate.get("system") or ""),
                        str(candidate.get("name") or ""),
                        str(candidate.get("title") or ""),
                        str(candidate.get("rom_name") or ""),
                        str(candidate.get("rom_path") or ""),
                        " ".join(str(field) for field in (candidate.get("missing") or [])),
                    ]
                ).lower()
                if normalized_query not in haystack:
                    continue
            filtered.append(candidate)

        result = self.repository.remove_gamelist_entries(filtered)
        result["matched_count"] = len(filtered)
        self._send_json(200, result)

    def _handle_public_image(self, system: str, image_file: str) -> None:
        if self.settings.use_fake_data:
            self._redirect_to_fake_image(seed=f"{system}-{image_file}", width=640, height=360)
            return
        system = valid_segment(unquote(system))
        system_dir = self.repository.get_system_dir(system)
        image_file = valid_segment(unquote(image_file))
        images_dir = (system_dir / "images").resolve()
        image_path = (images_dir / image_file).resolve()

        # Fast path: exact filename match.
        if image_path.exists() and image_path.is_file():
            self._stream_cached_image(image_path)
            return

        # Fallback 1: case-insensitive filename match in images root.
        if images_dir.exists() and images_dir.is_dir():
            requested_lower = image_file.lower()
            for candidate in images_dir.iterdir():
                if candidate.is_file() and candidate.name.lower() == requested_lower:
                    self._stream_cached_image(candidate.resolve())
                    return

        # Fallback 2: recursive stem-based lookup to handle theme/artwork packs
        # that use different extensions, case, or nested folders.
        requested_stem = Path(image_file).stem.lower()
        allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        preferred_stems = {
            requested_stem,
            requested_stem.replace("-image", ""),
            f"{requested_stem}-image",
        }
        checked = 0
        if images_dir.exists() and images_dir.is_dir():
            for candidate in images_dir.rglob("*"):
                checked += 1
                if checked > 30000:
                    break
                if not candidate.is_file():
                    continue
                if candidate.suffix.lower() not in allowed_suffixes:
                    continue
                if candidate.stem.lower() in preferred_stems:
                    self._stream_cached_image(candidate.resolve())
                    return

        # Some gamelist.xml files store artwork in nested media folders instead
        # of the standard images directory. Keep the lookup scoped to the system.
        checked = 0
        if system_dir.exists() and system_dir.is_dir():
            requested_lower = image_file.lower()
            for candidate in system_dir.rglob("*"):
                checked += 1
                if checked > 30000:
                    break
                if not candidate.is_file() or candidate.suffix.lower() not in allowed_suffixes:
                    continue
                if candidate.name.lower() == requested_lower:
                    self._stream_cached_image(candidate.resolve())
                    return

        raise FileNotFoundError()

    def _handle_public_video(self, system: str, rom_path: str) -> None:
        """Serve the ``<video>`` gamelist.xml reference for one specific ROM.

        Unlike ``_handle_public_image`` (which guesses a filename inside the
        system's ``images/`` folder), video files land in ``images/`` (manual
        upload) or ``videos/`` (P2P peer sync) depending on how they arrived --
        see ``test_artwork_video_lands_in_videos_subdir``. Resolving by the
        actual gamelist ``<video>`` reference via ``resolve_artwork_file``
        sidesteps that split instead of guessing both folders. Streamed via
        ``_stream_file`` (not ``_stream_cached_image``): videos are large
        enough that reading the whole file into the in-process image cache on
        every request would be wasteful on this device's limited memory.
        """
        system = valid_segment(unquote(system))
        target, _relative_path, _artwork_ref = self.repository.resolve_artwork_file(system, unquote(rom_path or ""), "video")
        self._stream_file(target, self._guess_content_type(target))

    def _handle_download(self, system: str, asset_type: str, unique_id: str) -> None:
        if not self.settings.downloads_enabled:
            raise ValueError("downloads are disabled")
        if asset_type == "roms" and str(system).strip().lower() == "steam":
            raise ValueError("steam rom downloads are disabled")
        unique_id = valid_segment(unique_id)
        asset_dir, items = self.repository.list_assets(system, asset_type)

        target_path = None
        is_downloadable = True
        for item in items:
            if item["unique_id"] == unique_id:
                file_name = str(item.get("rom_file") or item.get("name") or "")
                target_path = (asset_dir / file_name).resolve()
                is_downloadable = item.get("is_downloadable", True)
                break

        if not target_path or not target_path.exists():
            self.log_error(
                "download lookup failed system=%s asset_type=%s requested=%s resolved=%s reason=not_found",
                system,
                asset_type,
                unique_id,
                str(target_path) if target_path else "",
            )
            raise FileNotFoundError()
        if not is_downloadable:
            self.log_error("download lookup failed system=%s asset_type=%s requested=%s resolved=%s reason=not_downloadable", system, asset_type, unique_id, str(target_path))
            raise ValueError("asset is not downloadable")
        if not target_path.is_file():
            self.log_error("download lookup failed system=%s asset_type=%s requested=%s resolved=%s reason=not_file", system, asset_type, unique_id, str(target_path))
            raise ValueError("not a file")

        self._stream_file(target_path, "application/octet-stream", as_attachment=True)

    def _handle_image_file_or_download(self, system: str, image_ref: str) -> None:
        if self.settings.use_fake_data:
            self._redirect_to_fake_image(seed=f"{system}-{image_ref}", width=640, height=360)
            return
        system = valid_segment(unquote(system))
        system_dir = self.repository.get_system_dir(system)
        image_ref = valid_segment(unquote(image_ref))
        images_dir = (system_dir / "images").resolve()

        image_path = (images_dir / image_ref).resolve()
        if image_path.exists():
            if not image_path.is_file():
                raise ValueError("not a file")
            self._stream_cached_image(image_path)
            return

        _, roms = self.repository.list_assets(system, "roms")
        for rom in roms:
            if rom["unique_id"] == image_ref:
                stems: List[str] = []
                image_stem = rom.get("image_stem")
                if isinstance(image_stem, str) and image_stem:
                    stems.append(image_stem)
                name_stem = Path(rom["name"]).stem
                if name_stem not in stems:
                    stems.append(name_stem)
                source_folder = rom.get("source_folder")
                if isinstance(source_folder, str) and source_folder:
                    folder_stem = Path(source_folder).stem
                    if folder_stem not in stems:
                        stems.append(folder_stem)

                suffixes = [".png", ".jpg", ".jpeg", ".webp", ".gif"]
                name_patterns = ["{stem}-image{suffix}", "{stem}{suffix}"]
                for stem in stems:
                    for pattern in name_patterns:
                        for suffix in suffixes:
                            candidate_name = pattern.format(stem=stem, suffix=suffix)
                            mapped_image_path = (images_dir / candidate_name).resolve()
                            try:
                                self._stream_cached_image(mapped_image_path)
                                return
                            except FileNotFoundError:
                                continue

                # Fallback: recursive + case-insensitive match by stem for theme/artwork packs
                # that store images in subfolders or mixed-case extensions.
                allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                normalized_stems = {s.lower() for s in stems}
                normalized_stems_with_suffix = {f"{s.lower()}-image" for s in stems}
                checked = 0
                if images_dir.exists() and images_dir.is_dir():
                    for candidate in images_dir.rglob("*"):
                        checked += 1
                        if checked > 30000:
                            break
                        if not candidate.is_file():
                            continue
                        if candidate.suffix.lower() not in allowed_suffixes:
                            continue
                        candidate_stem = candidate.stem.lower()
                        if candidate_stem in normalized_stems or candidate_stem in normalized_stems_with_suffix:
                            self._stream_cached_image(candidate.resolve())
                            return
                raise FileNotFoundError()

        self._handle_download(system, "images", image_ref)
