"""Local emulator/Batocera config-file listing and reading.

Extracted from the retired Overmind reporting package (formerly
``overmind_reporting.py``) -- this half of that file was never hub-specific: it
backs the local admin "Emulators" config-file browser (``handlers_config.py``) and
the peer-served emulator-config listing (``handlers_peer.py``). The delta-upload
half of that file (change-only payloads with fingerprint cursors for the retired
heartbeat) was Overmind-only and was not carried forward.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_CONFIG_COLLECTION_SPECS = (
    (
        "main_config",
        "system",
        (
            "batocera.conf",
        ),
    ),
    (
        "emulator_config",
        "configs",
        (
            "amiberry/*.conf",
            "azahar/*.ini",
            "cemu/**/*",
            "citra-emu/*.ini",
            "dolphin-emu/*.ini",
            "dosbox/*.conf",
            "duckstation/*.ini",
            "flycast/emu.cfg",
            "mednafen/*.cfg",
            "melonds/*.ini",
            "model2emu/*.ini",
            "redream/*.cfg",
            "scummvm/*.ini",
            "supermodel/*.ini",
            "flycast/mappings/**/*",
            "mame/*.ini",
            "mame/*.cfg",
            "mupen64/**/*",
            "openbor/config*.ini",
            "PCSX2/**/*",
            "Play Data Files/config.xml",
            "play/Play Data Files/config.xml",
            "play/Play Data Files/inputprofiles/**/*",
            "ppsspp/PSP/SYSTEM/controls.ini",
            "ppsspp/PSP/SYSTEM/ppsspp.ini",
            "retroarch/**/*",
            "rpcs3/config.yml",
            "rpcs3/input_configs/**/*",
            "rpcs3/evdev_positive_axis.yml",
            "rpcs3/gem*.yml",
            "rpcs3/LogitechG27.yml",
            "rpcs3/usio.yml",
            "rpcs3/patches/patch.yml",
            "shadps4/user/config.toml",
            "shadps4/user/input_config/**/*",
            "vita3k/config.yml",
            "xemu/xemu.toml",
        ),
    ),
    (
        "batocera_config",
        "configs",
        (
            "emulationstation/es_input.cfg",
            "emulationstation/es_last_input.cfg",
            "emulationstation/es_settings.cfg",
            "emulationstation/es_systems.cfg",
            "emulationstation/es_systems_*.cfg",
            "emulationstation/es_features_steam.cfg",
            "emulationstation/es_systems_steam.cfg",
            "encoder_keys.conf",
            "multimedia_keys.conf",
            "antimicrox/antimicrox_settings.ini",
        ),
    ),
    (
        "desktop_or_ui_config",
        ".config",
        (
            "libfm/libfm.conf",
            "pcmanfm/**/*",
            "QtProject.conf",
            "yad.conf",
        ),
    ),
    (
        "patch_metadata",
        "configs",
        (
            "shadps4/user/patches/**/*",
            "rpcs3/patches/patch.yml",
        ),
    ),
)
_CONFIG_EXCLUSIONS = (
    "emulationstation/scrapers/**",
    "rpcs3/dev_flash/**",
    "rpcs3.broken.*",
    "rpcs3.broken.*/**",
    "rpcs3/players_history.yml",
    "rpcs3/recording.yml",
    "rpcs3/games.yml",
    "dolphin-emu/TimePlayed.ini",
    "dolphin-emu/Logger.ini",
    "shadps4/user/game_data/**",
    "shadps4/user/download/**",
    "shadps4/user/imgui.ini",
    "shadps4/user/qt_ui.ini",
)
_CONFIG_FILE_LIST_CACHE_TTL_SECONDS = 30.0
_CONFIG_FILE_LIST_CACHE: dict = {}


def _read_text_file(path: Path, max_bytes: int) -> dict:
    try:
        raw = path.read_bytes()[: max_bytes + 1]
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        return {
            "path": str(path),
            "size": path.stat().st_size,
            "truncated": truncated,
            "content": raw.decode("utf-8", errors="replace"),
        }
    except Exception as error:
        return {"path": str(path), "error": str(error)}


def _is_excluded_config_path(relative_path: str) -> bool:
    normalized = str(relative_path or "").replace("\\", "/").strip("/")
    lowered = normalized.lower()
    if ".bak" in lowered:
        return True
    if {"log", "logs"} & {part for part in lowered.split("/") if part}:
        return True
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in _CONFIG_EXCLUSIONS)


def _is_selected_config_relative(root_name: str, relative_path: str) -> bool:
    normalized = str(relative_path or "").replace("\\", "/").strip("/")
    if not normalized or _is_excluded_config_path(normalized):
        return False

    def matches(pattern: str) -> bool:
        if "**" in pattern:
            prefix, suffix = pattern.split("**", 1)
            prefix = prefix.rstrip("/")
            suffix = suffix.lstrip("/")
            if prefix and not normalized.startswith(f"{prefix}/"):
                return False
            if suffix in {"", "*"}:
                return True
        return fnmatch.fnmatchcase(normalized, pattern)

    return any(
        spec_root == root_name and any(matches(pattern) for pattern in patterns)
        for _category, spec_root, patterns in _CONFIG_COLLECTION_SPECS
    )


def _iter_pattern_files(root: Path, pattern: str):
    if "**" not in pattern:
        yield from root.glob(pattern)
        return

    prefix, suffix = pattern.split("**", 1)
    prefix = prefix.rstrip("/")
    suffix = suffix.lstrip("/")
    start = root / prefix if prefix else root
    if not start.exists() or not start.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = [
            name
            for name in dirnames
            if name.lower() not in {"log", "logs"}
            and not _is_excluded_config_path((Path(dirpath) / name).relative_to(root).as_posix())
        ]
        base = Path(dirpath)
        for filename in filenames:
            path = base / filename
            try:
                relative_path = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if suffix in {"", "*"} or fnmatch.fnmatchcase(relative_path, pattern):
                yield path


def _iter_selected_config_file_rows(settings: Any):
    """Yield configured Batocera files only, deduplicating cross-category paths."""
    roots = {
        "configs": settings.userdata_root / "system" / "configs",
        ".config": settings.userdata_root / "system" / ".config",
        "system": settings.userdata_root / "system",
    }
    selected = {}
    for _category, root_name, patterns in _CONFIG_COLLECTION_SPECS:
        root = roots[root_name]
        if not root.exists() or not root.is_dir():
            continue
        for pattern in patterns:
            for path in _iter_pattern_files(root, pattern):
                if not path.is_file():
                    continue
                relative_path = path.relative_to(root).as_posix()
                if _is_excluded_config_path(relative_path):
                    continue
                selected[str(path.resolve())] = {
                    "root_name": root_name,
                    "root": root,
                    "path": path,
                    "relative_path": relative_path,
                }
    for _, selected_row in sorted(selected.items(), key=lambda row: row[0].lower()):
        yield selected_row


def _selected_config_file_rows(settings: Any, use_cache: bool = True) -> list:
    roots = {
        "configs": settings.userdata_root / "system" / "configs",
        ".config": settings.userdata_root / "system" / ".config",
        "system": settings.userdata_root / "system",
    }
    cache_key = tuple((name, str(path), int(path.stat().st_mtime_ns) if path.exists() else 0) for name, path in sorted(roots.items()))
    now = time.monotonic()
    cached = _CONFIG_FILE_LIST_CACHE.get(cache_key)
    if use_cache and cached and now - float(cached.get("created_at") or 0) <= _CONFIG_FILE_LIST_CACHE_TTL_SECONDS:
        return [dict(row) for row in cached.get("rows", [])]
    rows = list(_iter_selected_config_file_rows(settings))
    if use_cache:
        _CONFIG_FILE_LIST_CACHE.clear()
        _CONFIG_FILE_LIST_CACHE[cache_key] = {"created_at": now, "rows": [dict(row) for row in rows]}
    return rows


def list_emulator_config_files(
    settings: Any,
    max_configs: int = 250,
    *,
    offset: int = 0,
    query: Optional[str] = None,
) -> dict:
    configs = []
    limit = max(0, int(max_configs or 0))
    safe_offset = max(0, int(offset or 0))
    normalized_query = str(query or "").strip().lower()
    selected_rows = _selected_config_file_rows(settings, use_cache=True)
    if normalized_query:
        selected_rows = [
            row for row in selected_rows
            if normalized_query in f"{row.get('root_name') or ''}/{row.get('relative_path') or ''}".lower()
        ]
    total = len(selected_rows)
    page_rows = selected_rows[safe_offset:safe_offset + limit] if limit else selected_rows[safe_offset:]
    for row in page_rows:
        path = row["path"]
        item = {
            "root_name": row["root_name"],
            "root": str(row["root"]),
            "relative_path": row["relative_path"],
            "path": str(path),
        }
        try:
            stat = path.stat()
            item["size"] = int(stat.st_size)
            item["modified_at"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat()
        except Exception as error:
            item["error"] = str(error)
        configs.append(item)
    return {
        "type": "emulator_configs",
        "configs": configs,
        "count": len(configs),
        "total": total,
        "max_configs": limit,
        "offset": safe_offset,
        "incremental": False,
    }


def read_emulator_config_file(settings: Any, root_name: str, relative_path: str, max_bytes: int = 131072) -> dict:
    requested_root = str(root_name or "").strip()
    requested_relative = str(relative_path or "").replace("\\", "/").strip().lstrip("/")
    safe_max_bytes = max(1024, min(int(max_bytes or 131072), 1048576))
    roots = {
        "configs": settings.userdata_root / "system" / "configs",
        ".config": settings.userdata_root / "system" / ".config",
        "system": settings.userdata_root / "system",
    }
    root = roots.get(requested_root)
    if root is None or not _is_selected_config_relative(requested_root, requested_relative):
        raise FileNotFoundError(f"Emulator config not found: {requested_root}:{requested_relative}")
    path = (root / requested_relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        raise FileNotFoundError(f"Emulator config not found: {requested_root}:{requested_relative}")
    if not path.is_file():
        raise FileNotFoundError(f"Emulator config not found: {requested_root}:{requested_relative}")
    item = _read_text_file(path, max_bytes=safe_max_bytes)
    item["root_name"] = requested_root
    item["root"] = str(root)
    item["relative_path"] = requested_relative
    try:
        fingerprint = hashlib.md5(path.read_bytes()).hexdigest()
    except Exception:
        fingerprint = hashlib.md5(str(item.get("content") or "").encode("utf-8", errors="replace")).hexdigest()
    item["md5"] = fingerprint
    item["fingerprint"] = fingerprint
    return item
