"""RomRepository asset + BIOS listing methods, as a mixin.

Extracted from ``drone_api.py``. Lists a system's ROM/BIOS/artwork assets (reusing the
cached snapshot), reports the BIOS root, lists BIOS entries (full-file MD5), and finds a
BIOS file by unique-id. Composed onto ``RomRepository`` (methods stay ``self``-bound).
"""

import hashlib
import json
import os
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from ..common.http_cache import valid_segment
    from ..storage.rom_metadata_store import _load_rom_metadata_cache, list_rom_rows_by_system, rom_cache_ready
    from .rom_metadata_state import _build_rom_metadata_snapshot_from_cache
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.http_cache import valid_segment  # type: ignore
    from storage.rom_metadata_store import _load_rom_metadata_cache, list_rom_rows_by_system, rom_cache_ready  # type: ignore
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

        for current_root, dirs, file_names in os.walk(bios_root):
            root_path = Path(current_root)

            for file_name in file_names:
                file_path = (root_path / file_name).resolve()
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

        for current_root, _, file_names in os.walk(bios_root):
            root_path = Path(current_root)
            for file_name in file_names:
                file_path = (root_path / file_name).resolve()
                if not file_path.is_file():
                    continue
                if self.build_unique_id(file_path) == unique_id:
                    return file_path

        raise FileNotFoundError()
