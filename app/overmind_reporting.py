"""Change-only payload collection for Drone-to-Overmind reporting."""

from __future__ import annotations

import fnmatch
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple

try:
    from .state_store import database_path, load_payload, save_payload
except ImportError:
    from state_store import database_path, load_payload, save_payload  # type: ignore


_STATE_SCHEMA_VERSION = 2
_CONFIG_COLLECTION_SPECS = (
    (
        "emulator_config",
        "configs",
        (
            "cemu/**/*",
            "dolphin-emu/*.ini",
            "dosbox/*.conf",
            "flycast/emu.cfg",
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


def _state_path(settings: Any, filename: str) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / filename).resolve()


def _read_delivery_state(settings: Any, filename: str, key: str) -> dict:
    state = load_payload(
        database_path(settings.userdata_root),
        filename,
        {},
        legacy_path=_state_path(settings, filename),
    )
    if not isinstance(state, dict) or state.get("schema_version") != _STATE_SCHEMA_VERSION:
        return {}
    values = state.get(key)
    return values if isinstance(values, dict) else {}


def _commit_delivery_state(settings: Any, filename: str, key: str, values: dict) -> None:
    save_payload(
        database_path(settings.userdata_root),
        filename,
        {"schema_version": _STATE_SCHEMA_VERSION, key: dict(values or {})},
    )


def load_uploaded_log_cursors(settings: Any) -> dict:
    return _read_delivery_state(settings, "overmind_log_cursors.json", "cursors")


def commit_log_cursors(settings: Any, cursors: dict) -> None:
    _commit_delivery_state(settings, "overmind_log_cursors.json", "cursors", cursors)


def load_uploaded_emulator_config_fingerprints(settings: Any) -> dict:
    return _read_delivery_state(settings, "overmind_config_fingerprints.json", "fingerprints")


def commit_emulator_config_fingerprints(settings: Any, fingerprints: dict) -> None:
    _commit_delivery_state(settings, "overmind_config_fingerprints.json", "fingerprints", fingerprints)


def _resolve_userdata_path(settings: Any, candidate: str) -> Path:
    if candidate == "/userdata":
        return settings.userdata_root.resolve()
    if candidate.startswith("/userdata/"):
        return (settings.userdata_root / candidate[len("/userdata/") :]).resolve()
    return Path(candidate).resolve()


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


def _read_text_file_delta(path: Path, cursor: dict, max_bytes: int) -> Tuple[dict, dict]:
    try:
        stat = path.stat()
        key = str(path.resolve())
        previous = cursor.get(key) if isinstance(cursor.get(key), dict) else {}
        previous_size = int(previous.get("size") or 0)
        size = int(stat.st_size)
        previous_mtime_ns = int(previous.get("mtime_ns") or 0)
        mtime_ns = int(stat.st_mtime_ns)
        skipped_bytes = 0
        if size > previous_size >= 0:
            start = previous_size
            if size - start > max_bytes:
                skipped_bytes = size - start - max_bytes
                start = size - max_bytes
        elif size == previous_size and mtime_ns == previous_mtime_ns:
            start = size
        else:
            start = max(0, size - max_bytes)
        with path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        content = raw.decode("utf-8", errors="replace")
        if skipped_bytes:
            content = f"[Log delivery skipped {skipped_bytes} older buffered bytes to show current output]\n{content}"
        next_cursor = {"size": start + len(raw), "mtime_ns": mtime_ns}
        return {
            "path": str(path),
            "size": size,
            "offset": start,
            "truncated": truncated,
            "content": content,
            "skipped_bytes": skipped_bytes,
            "delta": True,
        }, next_cursor
    except Exception as error:
        return {"path": str(path), "error": str(error), "delta": True}, {}


def collect_log_sources(settings: Any, include_unchanged: bool = False) -> dict:
    """Build changed log source payloads and deferred delivery cursors."""
    candidates = {
        "es_launch_stdout": ["/userdata/system/logs/es_launch_stdout.log"],
        "es_launch_stderr": ["/userdata/system/logs/es_launch_stderr.log"],
        "drone_stdout": [str((settings.log_dir / settings.stdout_log_file).resolve())],
        "drone_stderr": [str((settings.log_dir / settings.stderr_log_file).resolve())],
    }
    cursor = {} if include_unchanged else load_uploaded_log_cursors(settings)
    next_cursor = dict(cursor)
    logs = []
    for source, paths in candidates.items():
        entry = {"source": source, "files": []}
        for raw_path in paths:
            path = _resolve_userdata_path(settings, raw_path)
            if path.exists() and path.is_file():
                file_info, file_cursor = _read_text_file_delta(path, cursor, max_bytes=262144)
                if file_cursor:
                    next_cursor[str(path.resolve())] = file_cursor
                if str(file_info.get("content") or "") or file_info.get("error"):
                    entry["files"].append(file_info)
        if entry["files"]:
            logs.append(entry)
    return {
        "type": "log_sources",
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "logs": logs,
        "append": True,
        "_cursors": next_cursor,
    }


def _is_excluded_config_path(relative_path: str) -> bool:
    if ".bak" in relative_path.lower():
        return True
    return any(fnmatch.fnmatchcase(relative_path, pattern) for pattern in _CONFIG_EXCLUSIONS)


def _iter_selected_config_files(settings: Any):
    """Yield configured Batocera files only, deduplicating cross-category paths."""
    roots = {
        "configs": settings.userdata_root / "system" / "configs",
        ".config": settings.userdata_root / "system" / ".config",
    }
    selected = {}
    for _category, root_name, patterns in _CONFIG_COLLECTION_SPECS:
        root = roots[root_name]
        if not root.exists() or not root.is_dir():
            continue
        for pattern in patterns:
            for path in root.glob(pattern):
                if not path.is_file():
                    continue
                relative_path = path.relative_to(root).as_posix()
                if _is_excluded_config_path(relative_path):
                    continue
                selected[str(path.resolve())] = (root, path, relative_path)
    for _, selected_row in sorted(selected.items(), key=lambda row: row[0].lower()):
        yield selected_row


def collect_emulator_configs(settings: Any, include_unchanged: bool = False) -> dict:
    """Build changed emulator config payloads and deferred fingerprints."""
    previous_fingerprints = load_uploaded_emulator_config_fingerprints(settings)
    next_fingerprints = {}
    configs = []
    for root, path, relative_path in _iter_selected_config_files(settings):
        item = _read_text_file(path, max_bytes=131072)
        item["relative_path"] = relative_path
        item["root"] = str(root)
        key = f"{item['root']}:{item['relative_path']}"
        fingerprint = hashlib.sha256(str(item.get("content") or "").encode("utf-8", errors="replace")).hexdigest()
        changed = include_unchanged or previous_fingerprints.get(key) != fingerprint
        if changed and len(configs) < 250:
            item["fingerprint"] = fingerprint
            configs.append(item)
            next_fingerprints[key] = fingerprint
        elif changed:
            if key in previous_fingerprints:
                next_fingerprints[key] = previous_fingerprints[key]
        else:
            next_fingerprints[key] = fingerprint
    return {
        "type": "emulator_configs",
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "configs": configs,
        "changed": bool(configs),
        "incremental": not include_unchanged,
        "_fingerprints": next_fingerprints,
    }
