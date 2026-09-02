"""RomRepository asset + BIOS listing methods, as a mixin.

Extracted from ``drone_api.py``. Lists a system's ROM/BIOS/artwork assets (reusing the
cached snapshot), reports the BIOS root, lists BIOS entries (full-file MD5), and finds a
BIOS file by unique-id. Composed onto ``RomRepository`` (methods stay ``self``-bound).
"""

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from ..common.network_references import is_network_reference, network_reference_root
    from ..common.http_cache import valid_segment
    from ..storage.rom_metadata_store import (
        _load_rom_metadata_cache,
        delete_rom_cache_entry,
        get_rom_cache_row,
        list_bios_cache_page,
        list_rom_cache_page,
        list_rom_genre_counts,
        list_rom_rows_by_system,
        rom_cache_ready,
    )
    from .rom_metadata_state import _build_rom_metadata_snapshot_from_cache
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.network_references import is_network_reference, network_reference_root  # type: ignore
    from common.http_cache import valid_segment  # type: ignore
    from storage.rom_metadata_store import (  # type: ignore
        _load_rom_metadata_cache,
        delete_rom_cache_entry,
        get_rom_cache_row,
        list_bios_cache_page,
        list_rom_cache_page,
        list_rom_genre_counts,
        list_rom_rows_by_system,
        rom_cache_ready,
    )
    from roms.rom_metadata_state import _build_rom_metadata_snapshot_from_cache  # type: ignore


_BIOS_SYSTEM_MAP_PATH = Path(__file__).resolve().parent / "data" / "bios_system_map.json"
_BIOS_SYSTEM_MAP: Optional[dict] = None


def _load_bios_system_map() -> dict:
    """Load the vendored BIOS-md5 -> system_name(s) reference table once (see
    ``data/bios_system_map.json`` for provenance). Missing/corrupt file degrades to an
    empty map (every BIOS just reports no known system) rather than failing a scan."""
    global _BIOS_SYSTEM_MAP
    if _BIOS_SYSTEM_MAP is None:
        try:
            with _BIOS_SYSTEM_MAP_PATH.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            _BIOS_SYSTEM_MAP = data.get("md5_to_systems") if isinstance(data.get("md5_to_systems"), dict) else {}
        except Exception:
            _BIOS_SYSTEM_MAP = {}
    return _BIOS_SYSTEM_MAP


def bios_systems_for_md5(md5: Optional[str]) -> List[str]:
    """Return the system_name(s) a BIOS file with this MD5 is known to belong to, per
    the vendored reference table. Empty when the MD5 is unknown or ambiguous-free info
    isn't available -- most BIOS files won't match (the flat majority aren't in the
    reference set), which is expected, not an error."""
    key = str(md5 or "").strip().lower()
    if not key:
        return []
    systems = _load_bios_system_map().get(key)
    return list(systems) if isinstance(systems, list) else []


class RomAssetBiosMixin:
    def list_rom_assets_page(
        self,
        *,
        systems=None,
        query: str = "",
        genre: str = "",
        limit: int = 500,
        offset: int = 0,
        include_fingerprint: bool = True,
    ) -> Optional[dict]:
        """Return an SQLite-paged ROM inventory, enriched only for page rows."""
        if self.settings is None:
            return None
        page = list_rom_cache_page(
            self.settings,
            systems=systems,
            query=query,
            genre=genre,
            limit=limit,
            offset=offset,
        )
        if page is None:
            return None
        grouped: dict[str, list[dict]] = {}
        for item in page.get("items") or []:
            if not isinstance(item, dict):
                continue
            if not include_fingerprint:
                item.pop("fingerprint", None)
                item.pop("rom_fingerprint", None)
            item.pop("absolute_path", None)
            grouped.setdefault(str(item.get("system") or ""), []).append(item)
        for system, items in grouped.items():
            if not system:
                continue
            try:
                self._attach_gamelist_to_rom_items(self.get_system_dir(system), items)
            except Exception:
                continue
        return page

    def list_rom_browse_page(
        self,
        *,
        systems=None,
        genre: str = "",
        query: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> Optional[dict]:
        """The Systems Browse page's card grid: a plain SQLite-cache page,
        across every system by default. Deliberately skips
        list_rom_assets_page's per-row gamelist.xml re-attach -- a 200-row
        page drawing from potentially 200 different systems (round-robin
        ordering across all of them) would mean up to 200 separate gamelist
        parses per page load. The grid only needs name/system/image/genre,
        and both already come from what was indexed at scan time instead of
        a live re-parse: genre from the rom_genres table (see
        list_rom_cache_page's genre param), and the real gamelist-referenced
        image filename from image_relative_path (carried through
        RomCacheRow.extra -- see roms/gamelist.py's
        _database_rom_metadata_fields)."""
        if self.settings is None:
            return None
        page = list_rom_cache_page(self.settings, systems=systems, genre=genre, query=query, limit=limit, offset=offset)
        if page is None:
            return None
        for item in page.get("items") or []:
            if not isinstance(item, dict):
                continue
            item.pop("fingerprint", None)
            item.pop("rom_fingerprint", None)
            item.pop("absolute_path", None)
        return page

    def list_rom_genre_facets(self, *, systems=None, query: str = "") -> List[dict]:
        if self.settings is None:
            return []
        return list_rom_genre_counts(self.settings, systems=systems, query=query)

    def list_bios_page(
        self,
        *,
        query: str = "",
        folder_systems=None,
        known_system: str = "",
        unassigned: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> Optional[dict]:
        if self.settings is None:
            return None
        return list_bios_cache_page(
            self.settings,
            query=query,
            folder_systems=folder_systems,
            known_system=known_system,
            unassigned=unassigned,
            limit=limit,
            offset=offset,
        )

    def list_assets(self, system: str, asset_type: str, include_fingerprint: bool = True) -> Tuple[Path, List[dict]]:
        system_dir = self.get_system_dir(system)

        if asset_type == "roms":
            asset_dir = system_dir
        elif asset_type == "images":
            asset_dir = system_dir / "images"
        elif asset_type == "videos":
            asset_dir = system_dir / "videos"
        else:
            raise ValueError("invalid asset type")

        items = []
        # Fast path: query just this system's rows from SQLite (indexed by system),
        # instead of materializing the entire library snapshot in memory. Only used
        # once the cache is authoritative; otherwise we fall through to the filesystem.
        if asset_type == "roms" and self.settings is not None and rom_cache_ready(self.settings):
            rows = list_rom_rows_by_system(self.settings, system, include_fingerprint=include_fingerprint)
            if rows is not None:
                items = []
                for rom in rows:
                    relative_path = str(rom.get("file_path") or rom.get("rom_name") or "")
                    row = {
                        "unique_id": rom.get("unique_id") or hashlib.sha256(f"{system}:{relative_path}".encode("utf-8")).hexdigest()[:16],
                        "name": rom.get("rom_name") or Path(relative_path).name,
                        "rom_file": Path(relative_path).name,
                        "filename": Path(relative_path).name,
                        "relative_path": relative_path,
                        "rom_path": relative_path,
                        "file_path": relative_path,
                        "byte_count": rom.get("file_size"),
                        "entry_type": rom.get("entry_type") or "file",
                        "is_downloadable": rom.get("is_downloadable", True),
                        "image_stem": rom.get("image_stem") or Path(relative_path).stem,
                    }
                    # Folder-unit ROMs: peers need the folder + marker paths to fetch the
                    # whole game (relative_path stays the marker, the gamelist identity).
                    for key in ("transfer_unit_path", "marker_relative_path"):
                        if rom.get(key):
                            row[key] = str(rom[key])
                    if include_fingerprint:
                        row["fingerprint"] = rom.get("fingerprint")
                        row["rom_fingerprint"] = row["fingerprint"]
                    items.append(row)
                return system_dir, self._attach_gamelist_to_rom_items(system_dir, items)
        if asset_dir.exists() and asset_dir.is_dir():
            if asset_type == "roms":
                items = self._list_rom_items(system, asset_dir, include_fingerprint=include_fingerprint)
                items = self._attach_gamelist_to_rom_items(system_dir, items)
            else:
                for entry in self.iter_files(asset_dir):
                    stat = entry.stat()
                    items.append(
                        {
                            "unique_id": self.build_unique_id(entry),
                            "name": entry.name,
                            "byte_count": stat.st_size,
                            "entry_type": "file",
                            "is_downloadable": True,
                        }
                    )

        return asset_dir, items

    def delete_rom(self, system: str, unique_id: str) -> dict:
        """Permanently delete one ROM: the file (or, for a folder-unit ROM
        like the lindbergh/dreamcast marker-file games, the whole folder),
        its gamelist.xml entry, and its cache row -- the Systems Browse ROM
        detail page's delete action. An unknown (system, unique_id) is a
        no-op (``{"deleted": False}``), same convention as the movies-side
        delete. The file is unlinked *before* the cache row is removed: if
        the unlink fails, the row -- and so the ROM's visibility in the UI
        -- is left untouched rather than making a file that's still really
        on disk silently disappear until the next full rescan re-adds it."""
        unique_id = valid_segment(unique_id)
        row = get_rom_cache_row(self.settings, system, unique_id)
        if not row:
            return {"deleted": False}
        system_dir = self.get_system_dir(system).resolve()
        is_folder_unit = row["entry_type"] == "folder" and row["transfer_unit_path"]
        relative_path = row["transfer_unit_path"] if is_folder_unit else row["file_path"]
        if not relative_path:
            return {"deleted": False}
        target = (system_dir / relative_path).resolve()
        if target == system_dir or system_dir not in target.parents:
            return {"deleted": False}
        if is_folder_unit:
            shutil.rmtree(target, ignore_errors=False)
        else:
            target.unlink(missing_ok=True)
        try:
            self.remove_gamelist_entry(system, row["file_path"])
        except FileNotFoundError:
            pass  # never scraped / no gamelist entry -- not an error
        delete_rom_cache_entry(self.settings, row["entry_key"])
        return {"deleted": True, "file_path": row["file_path"], "rom_name": row["rom_name"]}

    def _cached_asset_snapshot(self) -> Optional[dict]:
        try:
            cache, rebuilt = _load_rom_metadata_cache(self.settings)
        except Exception:
            return None
        if rebuilt or not cache.get("last_full_scan_at") or cache.get("scan_in_progress"):
            return None
        if not isinstance(cache.get("systems"), list):
            return None
        return _build_rom_metadata_snapshot_from_cache(self.settings, cache)

    def get_bios_root(self) -> Path:
        if not self.bios_root.exists() or not self.bios_root.is_dir():
            raise FileNotFoundError()
        return self.bios_root.resolve()

    def is_local_bios_path(self, relative_path: str) -> bool:
        """Return whether a BIOS path is owned locally rather than referenced.

        This check is deliberately lexical: following a symlink into an
        unavailable network mount just to determine ownership could block the
        peer API.  Parent checks also cover a future layout that references a
        BIOS subdirectory instead of individual files.
        """
        relative = Path(str(relative_path or "").replace("\\", "/"))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            return False
        bios_root = self.get_bios_root()
        candidate = bios_root / relative
        share_root = network_reference_root()
        current = candidate
        while current != bios_root:
            if current.is_symlink() and is_network_reference(current, share_root):
                return False
            parent = current.parent
            if parent == current or bios_root not in parent.parents and parent != bios_root:
                return False
            current = parent
        return True

    def list_bios_entries(self) -> List[dict]:
        bios_root = self.get_bios_root()
        files: List[Tuple[Path, int]] = []
        allowed_extensions = {
            ".bin",
            ".rom",
            ".zip",
            ".img",
            ".keys",
            ".pup",
            ".gg",
            ".sms",
            ".pce",
            ".col",
            ".min",
            ".qcow2",
            ".nand",
            ".dat",
            ".iso",
            ".chd",
            ".7z",
        }

        share_root = network_reference_root()
        for current_root, dirs, file_names in os.walk(bios_root):
            root_path = Path(current_root)

            dirs[:] = [
                name for name in dirs
                if not ((root_path / name).is_symlink() and is_network_reference(root_path / name, share_root))
            ]

            for file_name in file_names:
                candidate = root_path / file_name
                if candidate.is_symlink() and is_network_reference(candidate, share_root):
                    continue
                file_path = candidate.resolve()
                if not file_path.is_file():
                    continue
                if not (file_path == bios_root or bios_root in file_path.parents):
                    continue
                if file_path.suffix.lower() not in allowed_extensions:
                    continue

                size = file_path.stat().st_size
                files.append((file_path, size))

        entries: List[dict] = []

        for file_path, size in sorted(files, key=lambda item: str(item[0].relative_to(bios_root)).lower()):
            relative_path = file_path.relative_to(bios_root).as_posix()
            # BIOS uses a full-file MD5 (exact emulator identity), not the sampled fingerprint.
            bios_md5 = self.build_md5(file_path)
            entries.append(
                {
                    "entry_type": "file",
                    "name": file_path.name,
                    "path": relative_path,
                    "unique_id": self.build_unique_id(file_path),
                    "byte_count": size,
                    "md5": bios_md5,
                    "bios_md5": bios_md5,
                    "systems": bios_systems_for_md5(bios_md5),
                }
            )

        return entries

    def find_bios_file_by_unique_id(self, unique_id: str) -> Path:
        unique_id = valid_segment(unique_id)
        bios_root = self.get_bios_root()
        share_root = network_reference_root()

        for current_root, dirs, file_names in os.walk(bios_root):
            root_path = Path(current_root)
            dirs[:] = [
                name for name in dirs
                if not ((root_path / name).is_symlink() and is_network_reference(root_path / name, share_root))
            ]
            for file_name in file_names:
                candidate = root_path / file_name
                if candidate.is_symlink() and is_network_reference(candidate, share_root):
                    continue
                file_path = candidate.resolve()
                if not file_path.is_file():
                    continue
                if self.build_unique_id(file_path) == unique_id:
                    return file_path

        raise FileNotFoundError()
