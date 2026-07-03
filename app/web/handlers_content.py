"""RomRequestHandler content + theme handlers, as a mixin.

Extracted from ``drone_api.py``. Serves the UI content listings (systems, ROM/BIOS/image/
video lists, BIOS download) and the theme metadata/backgrounds/logos/images/asset endpoints.
Composed onto ``RomRequestHandler`` (methods stay ``self``-bound).
"""

import re
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import quote, unquote

try:
    from ..common.http_cache import valid_segment
    from ..device.device_control import _resolve_es_systems_effective, _resolve_theme_dir
    from ..storage.rom_metadata_store import _load_rom_metadata_cache
    from .route_config import api_url
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.http_cache import valid_segment  # type: ignore
    from device.device_control import _resolve_es_systems_effective, _resolve_theme_dir  # type: ignore
    from storage.rom_metadata_store import _load_rom_metadata_cache  # type: ignore
    from web.route_config import api_url  # type: ignore


class HandlersContentMixin:
    def _handle_theme_meta(self) -> None:
        self._send_json(200, self._build_theme_meta(), cache_key="json:/theme/meta")

    def _build_system_theme_meta(self, system: str) -> dict:
        theme_dir = _resolve_theme_dir(self.settings)
        if not theme_dir:
            return {"enabled": False, "system": system, "reason": "no active theme"}

        candidate_dirs = [
            theme_dir / system,
            theme_dir / system.lower(),
            theme_dir / system.upper(),
            theme_dir / "default",
            theme_dir / "_inc",
        ]

        system_dir: Optional[Path] = None
        for candidate in candidate_dirs:
            if candidate.exists() and candidate.is_dir():
                system_dir = candidate.resolve()
                break

        if not system_dir:
            return {"enabled": False, "system": system, "reason": "system theme folder not found"}

        def first_match_recursive(base: Path, name_fragments: List[str], allowed_suffixes: Tuple[str, ...]) -> Optional[str]:
            checked = 0
            for path in base.rglob("*"):
                if checked > 5000:
                    break
                checked += 1
                if not path.is_file():
                    continue
                if path.suffix.lower() not in allowed_suffixes:
                    continue
                stem = path.stem.lower()
                if any(fragment in stem for fragment in name_fragments):
                    try:
                        return path.relative_to(theme_dir).as_posix()
                    except Exception:
                        continue
            return None

        theme_xml = first_match_recursive(system_dir, ["theme"], (".xml",))
        css_file = first_match_recursive(system_dir, ["style", "theme"], (".css",))
        bg_file = first_match_recursive(system_dir, ["background", "bg", "fond"], (".png", ".jpg", ".jpeg", ".webp"))
        logo_file = first_match_recursive(system_dir, ["logo", "title", "brand"], (".png", ".jpg", ".jpeg", ".webp", ".svg"))

        theme_xml_url = api_url(f"/theme/assets/{theme_xml}") if theme_xml else None
        css_url = api_url(f"/theme/assets/{css_file}") if css_file else None
        if self.settings.use_fake_data and css_url:
            css_url = None
        background_url = self._fake_theme_asset_url(bg_file) if (self.settings.use_fake_data and bg_file) else (api_url(f"/theme/assets/{bg_file}") if bg_file else None)
        logo_url = self._fake_theme_asset_url(logo_file) if (self.settings.use_fake_data and logo_file) else (api_url(f"/theme/assets/{logo_file}") if logo_file else None)

        return {
            "enabled": True,
            "system": system,
            "theme_name": theme_dir.name,
            "system_theme_dir": system_dir.relative_to(theme_dir).as_posix(),
            "theme_xml_url": theme_xml_url,
            "css_url": css_url,
            "background_url": background_url,
            "logo_url": logo_url,
            "resolved_files": {
                "theme_xml": theme_xml,
                "css": css_file,
                "background": bg_file,
                "logo": logo_file,
            },
        }

    def _handle_system_theme_meta(self, system: str) -> None:
        system = valid_segment(system)
        self._send_json(200, self._build_system_theme_meta(system), cache_key=f"json:/theme/system/{system}")

    def _build_theme_background_candidates(self) -> dict:
        theme_dir = _resolve_theme_dir(self.settings)
        if not theme_dir:
            return {
                "enabled": False,
                "theme_name": None,
                "count": 0,
                "backgrounds": [],
                "cache_seconds": 60,
            }

        allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
        # Mirrors requested shell filter semantics.
        path_pattern = re.compile(
            r"((_inc|assets|images|art|common).*(background|wallpaper|wall|back|bg))|"
            r"(/(background|wallpaper|wall|back|bg)[^/]*\.(png|jpg|jpeg|webp)$)",
            flags=re.IGNORECASE,
        )

        candidates: List[str] = []
        for path in theme_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in allowed_suffixes:
                continue
            rel = path.relative_to(theme_dir).as_posix()
            rel_with_slash = f"/{rel}"
            if path_pattern.search(rel_with_slash):
                candidates.append(rel)

        candidates = sorted(set(candidates), key=str.lower)
        if self.settings.use_fake_data:
            urls = [self._fake_theme_asset_url(rel) for rel in candidates]
        else:
            urls = [api_url(f"/theme/assets/{quote(rel, safe='/')}") for rel in candidates]
        return {
            "enabled": True,
            "theme_name": theme_dir.name,
            "count": len(urls),
            "backgrounds": urls,
            "cache_seconds": 60,
        }

    def _handle_theme_backgrounds(self) -> None:
        self._send_json(200, self._build_theme_background_candidates(), cache_key="json:/theme/backgrounds")

    def _build_theme_logo_candidates(self) -> dict:
        theme_dir = _resolve_theme_dir(self.settings)
        if not theme_dir:
            return {
                "enabled": False,
                "theme_name": None,
                "count": 0,
                "logos": [],
                "cache_seconds": 60,
            }

        allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
        name_pattern = re.compile(r"(logo|logos|system|wheel|marquee|banner)", flags=re.IGNORECASE)

        candidates: List[str] = []
        for path in theme_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in allowed_suffixes:
                continue
            rel = path.relative_to(theme_dir).as_posix()
            if name_pattern.search(rel):
                candidates.append(rel)

        candidates = sorted(set(candidates), key=str.lower)
        if self.settings.use_fake_data:
            urls = [self._fake_theme_asset_url(rel) for rel in candidates]
        else:
            urls = [api_url(f"/theme/assets/{quote(rel, safe='/')}") for rel in candidates]
        return {
            "enabled": True,
            "theme_name": theme_dir.name,
            "count": len(urls),
            "logos": urls[:200],
            "cache_seconds": 60,
        }

    def _handle_theme_logos(self) -> None:
        self._send_json(200, self._build_theme_logo_candidates(), cache_key="json:/theme/logos")

    def _build_theme_image_catalog(
        self,
        limit: int = 500,
        offset: int = 0,
        query: Optional[str] = None,
        system_filter: Optional[str] = None,
        system_filters: Optional[List[str]] = None,
    ) -> dict:
        theme_dir = _resolve_theme_dir(self.settings)
        if not theme_dir:
            return {"enabled": False, "theme_name": None, "count": 0, "images": []}

        allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}
        images_all: List[dict] = []
        checked = 0
        for path in theme_dir.rglob("*"):
            checked += 1
            if checked > 200000:
                break
            if not path.is_file():
                continue
            if path.suffix.lower() not in allowed_suffixes:
                continue
            rel = path.relative_to(theme_dir).as_posix()
            folder = Path(rel).parent.as_posix()
            image_url = self._fake_theme_asset_url(rel) if self.settings.use_fake_data else api_url(f"/theme/assets/{quote(rel, safe='/')}")
            images_all.append(
                {
                    "path": rel,
                    "folder": "." if folder == "." else folder,
                    "name": Path(rel).name,
                    "url": image_url,
                }
            )

        images_all.sort(key=lambda item: item["path"].lower())
        systems_all = sorted(
            {
                (item["folder"].split("/")[0] if item["folder"] != "." else "_root").lower()
                for item in images_all
            }
        )
        if query:
            q = query.strip().lower()
            images_all = [item for item in images_all if q in item["path"].lower()]
        selected_systems: List[str] = []
        if system_filters:
            selected_systems = [s.strip().lower() for s in system_filters if s and s.strip()]
        elif system_filter:
            selected_systems = [system_filter.strip().lower()]

        if "__none__" in selected_systems:
            images_all = []
        elif selected_systems:
            selected_set = set(selected_systems)
            images_all = [
                item
                for item in images_all
                if ((item["folder"].split("/")[0] if item["folder"] != "." else "_root").lower() in selected_set)
            ]

        total = len(images_all)
        offset = max(0, offset)
        limit = max(1, min(limit, 5000))
        images = images_all[offset : offset + limit]
        return {
            "enabled": True,
            "theme_name": theme_dir.name,
            "systems": systems_all,
            "count": total,
            "offset": offset,
            "limit": limit,
            "returned": len(images),
            "has_more": (offset + len(images)) < total,
            "images": images,
        }

    def _handle_theme_images(
        self,
        limit: int,
        offset: int,
        query: Optional[str],
        system_filter: Optional[str],
        system_filters: Optional[List[str]] = None,
    ) -> None:
        payload = self._build_theme_image_catalog(
            limit=limit,
            offset=offset,
            query=query,
            system_filter=system_filter,
            system_filters=system_filters,
        )
        systems_key = ",".join(sorted([s.lower() for s in (system_filters or [])]))
        cache_key = (
            f"json:/theme/images?limit={limit}&offset={offset}&q={(query or '').lower()}"
            f"&system={(system_filter or '').lower()}&systems={systems_key}"
        )
        self._send_json(200, payload, cache_key=cache_key)

    def _handle_theme_asset(self, relative_path: str) -> None:
        if self.settings.use_fake_data:
            lowered = relative_path.lower()
            if lowered.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")):
                self._redirect_to_fake_image(seed=f"theme-asset-{relative_path}", width=800, height=450)
                return
        theme_dir = _resolve_theme_dir(self.settings)
        if not theme_dir:
            raise FileNotFoundError()
        requested = unquote(relative_path.lstrip("/"))
        if not requested or "\x00" in requested:
            raise ValueError("invalid theme asset path")
        asset_path = (theme_dir / requested).resolve()
        if theme_dir not in asset_path.parents or not asset_path.exists() or not asset_path.is_file():
            raise FileNotFoundError()
        self._stream_file(asset_path, self._guess_content_type(asset_path))

    def _handle_systems(self) -> None:
        systems = self.repository.list_systems()
        _, es_systems = _resolve_es_systems_effective(self.settings)
        if es_systems:
            visible = {
                str(item.get("name", "")).strip().lower()
                for item in es_systems
                if item.get("name") and not bool(item.get("hidden"))
            }
            if visible:
                systems = [item for item in systems if str(item.get("name", "")).lower() in visible]
        self._send_json(200, {"systems": systems}, cache_key="json:/systems")

    def _handle_rom_list(self, system: str) -> None:
        _, roms = self.repository.list_assets(system, "roms", include_fingerprint=False)
        if not self.settings.downloads_enabled:
            for item in roms:
                item["is_downloadable"] = False
        self._send_json(200, {"system": system, "roms": roms}, cache_key=f"json:/systems/{system}?fingerprint=0")

    def _handle_images_list(self, system: str) -> None:
        _, images = self.repository.list_assets(system, "images")
        self._send_json(
            200,
            {"system": system, "images": images},
            cache_key=f"json:/systems/{system}/images",
        )

    def _handle_videos_list(self, system: str) -> None:
        _, videos = self.repository.list_assets(system, "videos")
        self._send_json(
            200,
            {"system": system, "videos": videos},
            cache_key=f"json:/systems/{system}/videos",
        )

    def _handle_bios_list(
        self,
        limit: int = 100,
        offset: int = 0,
        query: Optional[str] = None,
        system_filters: Optional[List[str]] = None,
    ) -> None:
        cache, _ = _load_rom_metadata_cache(self.settings)
        cached_bios = cache.get("bios_entries") if isinstance(cache.get("bios_entries"), dict) else {}
        entries = []
        for row in cached_bios.values():
            if not isinstance(row, dict):
                continue
            path = str(row.get("file_path") or row.get("relative_path") or row.get("path") or "").strip()
            if not path:
                continue
            md5_value = str(row.get("bios_md5") or row.get("md5") or "").strip()
            entries.append({
                **row,
                "name": row.get("name") or Path(path).name,
                "path": path,
                "byte_count": row.get("byte_count") if row.get("byte_count") is not None else row.get("file_size"),
                "fingerprint": md5_value,
                "md5": md5_value,
                "bios_md5": md5_value,
            })
        if not entries:
            entries = self.repository.list_bios_entries()
        query_value = (query or "").strip().lower()
        selected_systems = set((s or "").strip().lower() for s in (system_filters or []) if (s or "").strip())
        none_selected = "__none__" in selected_systems
        selected_systems.discard("__none__")

        def _entry_system(item: dict) -> str:
            path = item.get("path") or item.get("name") or ""
            return (path.split("/")[0] if "/" in path else "_root").lower()

        systems_all = sorted({_entry_system(item) for item in entries})
        filtered = entries
        if query_value:
            filtered = [
                item
                for item in filtered
                if (
                    query_value in (item.get("path") or "").lower()
                    or query_value in (item.get("name") or "").lower()
                    or query_value in (item.get("fingerprint") or "").lower()
                    or query_value in _entry_system(item)
                )
            ]
        if none_selected:
            filtered = []
        elif selected_systems:
            filtered = [item for item in filtered if _entry_system(item) in selected_systems]

        total = len(filtered)
        offset = max(0, offset)
        limit = max(1, min(limit, 5000))
        page_entries = filtered[offset : offset + limit]

        if not self.settings.downloads_enabled:
            for item in page_entries:
                item["is_downloadable"] = False
        else:
            for item in page_entries:
                item["is_downloadable"] = True
        systems_filtered = sorted({_entry_system(item) for item in filtered})
        cache_key = (
            f"json:/bios?limit={limit}&offset={offset}&q={query_value}"
            f"&systems={','.join(sorted(selected_systems))}"
        )
        self._send_json(
            200,
            {
                "bios": page_entries,
                "count": total,
                "offset": offset,
                "limit": limit,
                "returned": len(page_entries),
                "has_more": (offset + len(page_entries)) < total,
                "systems": systems_all,
                "systems_filtered": systems_filtered,
            },
            cache_key=cache_key,
        )

    def _handle_bios_download(self, unique_id: str) -> None:
        if not self.settings.downloads_enabled:
            raise ValueError("downloads are disabled")
        target_path = self.repository.find_bios_file_by_unique_id(unique_id)
        self._stream_file(target_path, "application/octet-stream", as_attachment=True)
