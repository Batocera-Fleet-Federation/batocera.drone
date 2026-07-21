"""Gamelist XML + ROM-metadata-field helpers.

Extracted from ``drone_api.py``. Pure helpers for reading Batocera ``gamelist.xml``
entries (title/desc/genre/rating/image paths + a stable per-game id), building the
ROM-metadata dict the cache consumes, writing entries back, and small
artwork/placeholder-image utilities. Pure stdlib (``xml.etree`` + ``hashlib``); holds
no Drone state.
"""

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# The gamelist.xml artwork fields the Drone reads/writes (also the valid artwork_type
# values). Shared widely; re-exported from drone_api for back-compat.
ARTWORK_FIELDS = ("image", "thumbnail", "marquee", "fanart", "boxart", "video", "wheel", "manual")
# Sentinel artwork-filter value meaning "entries whose artwork is a duplicate".
ARTWORK_DUPLICATE_FILTER = "duplicate_artwork"


def _text_or_empty(parent: ET.Element, tag: str) -> str:
    child = parent.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _gamelist_details(game: Optional[ET.Element]) -> dict:
    if game is None:
        return {}
    details = {}
    for child in list(game):
        tag = child.tag
        value = (child.text or "").strip()
        if child.attrib:
            value = {"text": value, "attributes": dict(child.attrib)}
        if tag in details:
            existing = details[tag]
            if isinstance(existing, list):
                existing.append(value)
            else:
                details[tag] = [existing, value]
        else:
            details[tag] = value
    return details


def _gamelist_game_id(game: Optional[ET.Element], relative_path: str) -> str:
    if game is None:
        return relative_path
    return str(game.get("id") or _text_or_empty(game, "id") or relative_path).strip() or relative_path


def _find_gamelist_entry_by_game_id(root: ET.Element, game_id: str) -> Optional[ET.Element]:
    wanted = str(game_id or "").strip()
    if not wanted:
        return None
    wanted_path = _normalize_gamelist_rom_path(wanted).lower()
    for game in root.findall("game"):
        candidates = [
            str(game.get("id") or "").strip(),
            _text_or_empty(game, "id"),
            _normalize_gamelist_rom_path(_text_or_empty(game, "path")),
        ]
        if any(candidate and candidate == wanted for candidate in candidates):
            return game
        if wanted_path and any(_normalize_gamelist_rom_path(candidate).lower() == wanted_path for candidate in candidates):
            return game
    return None


def _gamelist_metadata_for_reference(gamelist_path: str, game_id: str) -> dict:
    path = Path(str(gamelist_path or ""))
    if not path.exists() or not path.is_file():
        return {}
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return {}
    if root.tag != "gameList":
        return {}
    game = _find_gamelist_entry_by_game_id(root, game_id)
    return _gamelist_details(game)


def _database_rom_metadata_fields(rom: dict, system_name: str, file_path: str, absolute: Path, stat_size: int, stat_mtime: int) -> dict:
    display_name = Path(file_path).stem
    return {
        **{k: v for k, v in rom.items() if k not in {"fingerprint", "rom_fingerprint", "gamelist", "existing", "name", "title", "rom_name"}},
        "system": system_name,
        "system_name": system_name,
        "rom_name": display_name,
        "name": display_name,
        "title": display_name,
        "rom_file": Path(file_path).name,
        "filename": Path(file_path).name,
        "file_path": file_path,
        "relative_path": file_path,
        "rom_path": file_path,
        "file_size": stat_size,
        "byte_count": stat_size,
        "size": stat_size,
        "modified_time": stat_mtime,
        "mtime": stat_mtime,
        "absolute_path": str(absolute),
        "source": "gamelist.xml" if rom.get("gamelist_path") else "filesystem",
        "metadata_source": "gamelist.xml" if rom.get("gamelist_path") else "filesystem",
        "has_gamelist_entry": bool(rom.get("gamelist_path")),
        "image_stem": display_name,
    }


def _set_child_text(parent: ET.Element, tag: str, value: str) -> None:
    child = parent.find(tag)
    if child is None:
        child = ET.SubElement(parent, tag)
    child.text = value


def _first_metadata_value(*values) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            nested = _first_metadata_value(
                value.get("name"),
                value.get("title"),
                value.get("value"),
                value.get("displayName"),
            )
            if nested:
                return nested
            continue
        if isinstance(value, (list, tuple, set)):
            parts = [_first_metadata_value(item) for item in value]
            joined = ", ".join(part for part in parts if part)
            if joined:
                return joined
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _looks_like_placeholder_image(data: bytes) -> bool:
    """Catch common tiny/blank scraper placeholders before assigning them to ROM art."""
    if not data or len(data) < 128:
        return True
    sample = data[: min(len(data), 8192)]
    if sample and len(set(sample)) <= 3:
        return True
    digest = hashlib.sha256(data).hexdigest()
    known_bad = {
        # LaunchBox and CDN placeholders can shift, so keep this list small and
        # combine it with the tiny/flat image checks above.
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    }
    return digest in known_bad


def _artwork_identity(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/").lower()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.netloc}{parsed.path}".rstrip("/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw.lstrip("/")


def _remove_child(parent: ET.Element, tag: str) -> None:
    child = parent.find(tag)
    if child is not None:
        parent.remove(child)


def _relative_artwork_path(system_dir: Path, path: Path) -> str:
    try:
        return f"./{path.resolve().relative_to(system_dir.resolve()).as_posix()}"
    except Exception:
        return str(path)


def _normalize_gamelist_rom_path(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw.lstrip("/")
