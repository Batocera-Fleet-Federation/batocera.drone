"""Gameplay session parsing and payload assembly for Overmind reporting."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

try:
    from .state_store import database_path, load_payload, save_payload
except ImportError:
    from state_store import database_path, load_payload, save_payload  # type: ignore


LogCollector = Callable[[Any], dict]
ErrorFormatter = Callable[[BaseException], str]
_STATE_SCHEMA_VERSION = 2


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


GAME_EVENT_SPOOL_DIRNAME = "game-events"


def _game_event_spool_dir(settings: Any) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / GAME_EVENT_SPOOL_DIRNAME).resolve()


def _event_duration_seconds(start_iso: str, end_iso: str) -> Optional[int]:
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
    except (TypeError, ValueError):
        return None
    delta = int((end - start).total_seconds())
    return delta if delta >= 0 else None


def collect_game_event_sessions(
    settings: Any,
    repository: Optional[Any] = None,
    *,
    format_error: Optional[ErrorFormatter] = None,
) -> Tuple[List[dict], List[Path]]:
    """Drain the EmulationStation game-event spool into Overmind game sessions.

    Each file in the spool is one launch/stop event written by the ES hook
    (``drone-game-event.sh``). Returns the sessions to report plus the list of
    spool files consumed, so the caller can delete them only after a successful
    upload. ``game-start`` yields a session immediately; a matching ``game-end``
    in the same batch fills in ``duration_seconds``.
    """
    formatter = format_error or (lambda error: str(error))
    spool = _game_event_spool_dir(settings)
    if not spool.exists() or not spool.is_dir():
        return [], []
    try:
        files = sorted(path for path in spool.iterdir() if path.is_file() and path.suffix == ".json")
    except OSError:
        return [], []

    collected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    sessions: List[dict] = []
    processed: List[Path] = []
    starts: dict = {}  # resolved rom path -> (session index, started_at) for in-batch duration
    for path in files:
        processed.append(path)
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        rom_value = str(event.get("rom_path") or "").strip()
        if not rom_value:
            continue
        kind = str(event.get("event") or "start").strip().lower()
        played_at = _parse_launch_timestamp(str(event.get("played_at") or ""), collected_at)
        rom_path = _resolve_launch_rom_path(settings, "", rom_value)
        resolved_rom = rom_path.as_posix() if rom_path else rom_value

        if kind == "end":
            pending = starts.get(resolved_rom)
            if pending is not None:
                index, started_at = pending
                duration = _event_duration_seconds(started_at, played_at)
                if duration is not None:
                    sessions[index]["duration_seconds"] = duration
            continue

        system_name = _system_from_launch_rom_path(settings, rom_path, "")
        game_name = Path(rom_value).name
        if not system_name or not game_name:
            continue
        session = {
            "played_at": played_at,
            "system_name": system_name,
            "game_name": game_name,
            "rom_path": resolved_rom,
        }
        if rom_path and repository:
            try:
                session["rom_fingerprint"] = repository.build_fingerprint(rom_path)
            except Exception as error:  # noqa: BLE001 - fingerprint is best-effort
                session["rom_fingerprint_error"] = formatter(error)
        sessions.append(session)
        starts[resolved_rom] = (len(sessions) - 1, played_at)
    return sessions, processed


def delete_game_event_spool(files: Optional[List[Path]]) -> None:
    """Remove spool files that have been successfully reported to Overmind."""
    for path in files or []:
        try:
            Path(path).unlink()
        except OSError:
            pass


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
    seen_sessions = set()
    collected_at = log_data["collected_at"]

    def append_session(session: dict) -> None:
        if not session.get("system_name") or not session.get("game_name"):
            return
        key = (
            str(session.get("played_at") or ""),
            str(session.get("system_name") or ""),
            str(session.get("game_name") or ""),
            str(session.get("rom_path") or ""),
        )
        if key in seen_sessions:
            return
        seen_sessions.add(key)
        sessions.append(dict(session))

    for source in log_data.get("logs", []):
        if source.get("source") != "es_launch_stdout":
            continue
        for file_info in source.get("files", []):
            current = {}
            for line in str(file_info.get("content") or "").splitlines():
                lowered = line.lower()
                if "start_rom running system:" in lowered:
                    current["played_at"] = _parse_launch_timestamp(line, current.get("played_at") or collected_at)
                    match = re.search(r"running system:\s*([^\s]+)", line, re.IGNORECASE)
                    if match:
                        current["system_name"] = match.group(1).strip()
                if "game settings name:" in lowered:
                    current["played_at"] = _parse_launch_timestamp(line, current.get("played_at") or collected_at)
                    match = re.search(r"game settings name:\s*(.+)$", line, re.IGNORECASE)
                    if match:
                        current["game_name"] = Path(match.group(1).strip()).name
                if "callexternalscripts" in lowered and "gamestart" in lowered and "/userdata/roms/" in line:
                    current["played_at"] = _parse_launch_timestamp(line, current.get("played_at") or collected_at)
                    system_match = re.search(r"['\"]gameStart['\"]\s*,\s*['\"]([^'\"]+)['\"]", line)
                    rom_match = re.search(r"PosixPath\('([^']*/userdata/roms/[^']+)'\)", line)
                    if not rom_match:
                        rom_match = re.search(r"['\"](/userdata/roms/[^'\"]+)['\"]", line)
                    if system_match:
                        current["system_name"] = system_match.group(1).strip()
                    if rom_match:
                        rom_value = rom_match.group(1).strip()
                        rom_path = _resolve_launch_rom_path(settings, str(current.get("system_name") or ""), rom_value)
                        system_name = _system_from_launch_rom_path(settings, rom_path, str(current.get("system_name") or ""))
                        current["system_name"] = system_name
                        current["rom_path"] = rom_path.as_posix() if rom_path else rom_value
                        current["game_name"] = Path(rom_value).name
                        if rom_path and repository:
                            try:
                                current["rom_fingerprint"] = repository.build_fingerprint(rom_path)
                            except Exception as error:
                                current["rom_fingerprint_error"] = formatter(error)
                        append_session(current)
                        current = {}
                        continue
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
                                current["rom_fingerprint"] = repository.build_fingerprint(rom_path)
                            except Exception as error:
                                current["rom_fingerprint_error"] = formatter(error)
                    if current.get("system_name") and current.get("game_name"):
                        append_session(current)
                        current = {}
            if current.get("system_name") and current.get("game_name"):
                append_session(current)
    return {
        "type": "game_logs",
        "collected_at": collected_at,
        "sessions": sessions,
        "logs": log_data.get("logs", []),
        "_cursors": log_data.get("_cursors", {}),
    }
