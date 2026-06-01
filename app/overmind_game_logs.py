"""Gameplay session parsing and payload assembly for Overmind reporting."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from .state_store import database_path, load_payload, save_payload
except ImportError:
    from state_store import database_path, load_payload, save_payload  # type: ignore


LogCollector = Callable[[Any], dict]
ErrorFormatter = Callable[[BaseException], str]
_STATE_SCHEMA_VERSION = 1


def _state_path(settings: Any, filename: str) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / filename).resolve()


def load_game_log_cursors(settings: Any) -> dict:
    state = load_payload(
        database_path(settings.userdata_root),
        "overmind_game_log_cursors.json",
        {},
        legacy_path=_state_path(settings, "overmind_game_log_cursors.json"),
    )
    if not isinstance(state, dict) or state.get("schema_version") != _STATE_SCHEMA_VERSION:
        return {}
    cursors = state.get("cursors")
    return cursors if isinstance(cursors, dict) else {}


def commit_game_log_cursors(settings: Any, cursors: dict) -> None:
    save_payload(
        database_path(settings.userdata_root),
        "overmind_game_log_cursors.json",
        {"schema_version": _STATE_SCHEMA_VERSION, "cursors": dict(cursors or {})},
    )


def _resolve_userdata_path(settings: Any, candidate: str) -> Path:
    if candidate == "/userdata":
        return settings.userdata_root.resolve()
    if candidate.startswith("/userdata/"):
        return (settings.userdata_root / candidate[len("/userdata/") :]).resolve()
    return Path(candidate).resolve()


def _read_launch_log_delta(settings: Any, max_bytes: int = 262144) -> dict:
    path = _resolve_userdata_path(settings, "/userdata/system/logs/es_launch_stdout.log")
    collected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cursor = load_game_log_cursors(settings)
    next_cursor = dict(cursor)
    if not path.exists() or not path.is_file():
        return {"type": "log_sources", "collected_at": collected_at, "logs": [], "_cursors": next_cursor}
    try:
        stat = path.stat()
        key = str(path.resolve())
        previous = cursor.get(key) if isinstance(cursor.get(key), dict) else {}
        previous_size = int(previous.get("size") or 0)
        previous_mtime_ns = int(previous.get("mtime_ns") or 0)
        size = int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)
        if size > previous_size >= 0:
            start = previous_size
        elif size == previous_size and mtime_ns == previous_mtime_ns:
            start = size
        else:
            start = 0
        if size - start > max_bytes:
            start = size - max_bytes
        with path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read(max_bytes)
        next_cursor[key] = {"size": start + len(raw), "mtime_ns": mtime_ns}
        content = raw.decode("utf-8", errors="replace")
        if not content:
            return {"type": "log_sources", "collected_at": collected_at, "logs": [], "_cursors": next_cursor}
        return {
            "type": "log_sources",
            "collected_at": collected_at,
            "logs": [{"source": "es_launch_stdout", "files": [{"path": str(path), "content": content, "offset": start}]}],
            "_cursors": next_cursor,
        }
    except Exception as error:
        return {
            "type": "log_sources",
            "collected_at": collected_at,
            "logs": [{"source": "es_launch_stdout", "files": [{"path": str(path), "error": str(error)}]}],
            "_cursors": next_cursor,
        }


def _parse_launch_timestamp(line: str, fallback: str) -> str:
    patterns = [
        r"(?P<stamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)",
        r"\[(?P<stamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\]",
    ]
    for pattern in patterns:
        match = re.search(pattern, line)
        if not match:
            continue
        value = match.group("stamp").replace(",", ".")
        try:
            parsed = datetime.fromisoformat(value.replace(" ", "T"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        except ValueError:
            continue
    return fallback


def _resolve_launch_rom_path(settings: Any, system_name: str, rom_value: str) -> Optional[Path]:
    rom_text = str(rom_value or "").strip().strip('"')
    if not rom_text:
        return None
    candidates = [Path(rom_text)]
    if rom_text.startswith("/userdata/"):
        candidates.append((settings.userdata_root / rom_text[len("/userdata/") :]).resolve())
    if system_name:
        candidates.append((settings.roms_root / system_name / rom_text).resolve())
    candidates.append((settings.roms_root / rom_text).resolve())
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        if resolved.exists() and resolved.is_file():
            return resolved
    return None


def _system_from_launch_rom_path(settings: Any, rom_path: Optional[Path], fallback: str) -> str:
    fallback = str(fallback or "").strip()
    if fallback:
        return fallback
    if not rom_path:
        return ""
    try:
        relative = rom_path.resolve().relative_to(settings.roms_root.resolve())
        return relative.parts[0] if relative.parts else ""
    except Exception:
        return ""


def collect_game_logs(
    settings: Any,
    repository: Optional[Any] = None,
    log_data: Optional[dict] = None,
    *,
    collect_log_sources: Optional[LogCollector] = None,
    format_error: Optional[ErrorFormatter] = None,
) -> dict:
    """Build game sessions from EmulationStation launch output."""
    if not log_data:
        log_data = _read_launch_log_delta(settings)
    formatter = format_error or (lambda error: str(error))
    sessions = []
    collected_at = log_data["collected_at"]
    for source in log_data.get("logs", []):
        if source.get("source") != "es_launch_stdout":
            continue
        for file_info in source.get("files", []):
            current = {}
            for line in str(file_info.get("content") or "").splitlines():
                lowered = line.lower()
                if "emulator=" in lowered:
                    current["raw_emulator_line"] = line
                    match = re.search(r"emulator=([^\s]+)", line, re.IGNORECASE)
                    if match:
                        current["system_name"] = match.group(1)
                if "rom=" in lowered:
                    current["raw_rom_line"] = line
                    current["played_at"] = _parse_launch_timestamp(line, current.get("played_at") or collected_at)
                    match = re.search(r"rom=(.+)$", line, re.IGNORECASE)
                    if match:
                        rom_value = match.group(1).strip()
                        rom_path = _resolve_launch_rom_path(settings, str(current.get("system_name") or ""), rom_value)
                        system_name = _system_from_launch_rom_path(settings, rom_path, str(current.get("system_name") or ""))
                        current["system_name"] = system_name
                        current["rom_path"] = rom_path.as_posix() if rom_path else rom_value
                        current["game_name"] = Path(rom_value).name
                        if rom_path and repository:
                            try:
                                current["rom_md5"] = repository.build_md5(rom_path)
                            except Exception as error:
                                current["rom_md5_error"] = formatter(error)
                    if current.get("system_name") and current.get("game_name"):
                        sessions.append(dict(current))
                        current = {}
    return {
        "type": "game_logs",
        "collected_at": collected_at,
        "sessions": sessions,
        "logs": log_data.get("logs", []),
        "_cursors": log_data.get("_cursors", {}),
    }
