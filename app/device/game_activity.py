"""Gameplay session detection and durable history tracking.

Moved from the retired Overmind reporting package (formerly ``overmind_game_logs.py``) --
none of this is hub-specific. ``find_running_emulatorlauncher`` backs idle-game-exit
automation and the local admin/peer "is a game running" checks; the gameplay-history
functions back the local admin gameplay-history viewer and the peer-browsable
``type=gameplay`` inventory.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

try:
    from ..storage.state_store import database_path, load_payload, open_database, save_payload
except ImportError:
    from storage.state_store import database_path, load_payload, open_database, save_payload  # type: ignore


LogCollector = Callable[[Any], dict]
ErrorFormatter = Callable[[BaseException], str]
_STATE_SCHEMA_VERSION = 2
GAMEPLAY_HISTORY_NAMESPACE = "gameplay_history"
GAMEPLAY_HISTORY_MIGRATION_NAMESPACE = "gameplay_history_relational_migration"


def _state_path(settings: Any, filename: str) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / filename).resolve()


def load_game_log_cursors(settings: Any) -> dict:
    state = load_payload(
        database_path(settings.userdata_root),
        "game_log_cursors.json",
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
        "game_log_cursors.json",
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


def _parse_emulatorlauncher_args(parts: List[str], cmdline: str) -> Optional[dict]:
    if not any("emulatorlauncher" in part.lower() for part in parts):
        return None
    system_name = ""
    rom_path = ""
    for index, part in enumerate(parts):
        if part == "-system" and index + 1 < len(parts):
            system_name = parts[index + 1]
        elif part == "-rom" and index + 1 < len(parts):
            rom_path = parts[index + 1]
    if not rom_path:
        return None
    return {"system_name": system_name, "rom_path": rom_path, "cmdline": str(cmdline or "")}


def parse_emulatorlauncher_command(cmdline: str) -> Optional[dict]:
    """Extract the active Batocera system and ROM from a printable command line."""
    try:
        parts = shlex.split(str(cmdline or ""))
    except ValueError:
        parts = str(cmdline or "").split()
    return _parse_emulatorlauncher_args(parts, str(cmdline or ""))


def find_running_emulatorlauncher(proc_root: Path = Path("/proc")) -> Optional[dict]:
    """Return the first active emulatorlauncher invocation from Linux procfs."""
    try:
        pids = sorted((path for path in proc_root.iterdir() if path.name.isdigit()), key=lambda path: int(path.name))
    except OSError:
        return None
    for pid_path in pids:
        try:
            raw = (pid_path / "cmdline").read_bytes()
        except OSError:
            continue
        parts = [part.decode("utf-8", errors="ignore") for part in raw.split(b"\x00") if part]
        cmdline = " ".join(shlex.quote(part) for part in parts)
        parsed = _parse_emulatorlauncher_args(parts, cmdline)
        if parsed:
            parsed["pid"] = int(pid_path.name)
            return parsed
    return None


def write_game_process_event(settings: Any, kind: str, game: dict) -> Optional[Path]:
    """Atomically append a process-detected game start/stop event to the durable spool."""
    spool = _game_event_spool_dir(settings)
    try:
        spool.mkdir(parents=True, exist_ok=True)
        event_id = f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex[:8]}-{kind}"
        temp_path = spool / f".{event_id}.tmp"
        final_path = spool / f"{event_id}.json"
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        payload = {
            "event": kind,
            "played_at": str(game.get("started_at") or now) if kind == "start" else now,
            "started_at": str(game.get("started_at") or ""),
            "system_name": str(game.get("system_name") or ""),
            "rom_path": str(game.get("rom_path") or ""),
            "source": "emulatorlauncher_process",
        }
        temp_path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
        temp_path.replace(final_path)
        return final_path
    except OSError:
        return None


class GameProcessMonitor:
    """Poll procfs and emit durable events when emulatorlauncher starts or stops."""

    def __init__(self, settings: Any, poll_seconds: float = 2.0, proc_root: Path = Path("/proc")) -> None:
        self.settings = settings
        self.poll_seconds = max(0.25, float(poll_seconds))
        self.proc_root = proc_root
        self.active_game: Optional[dict] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def poll_once(self) -> Optional[dict]:
        current = find_running_emulatorlauncher(self.proc_root)
        active_key = (
            str((self.active_game or {}).get("system_name") or ""),
            str((self.active_game or {}).get("rom_path") or ""),
        )
        current_key = (
            str((current or {}).get("system_name") or ""),
            str((current or {}).get("rom_path") or ""),
        )
        if self.active_game and active_key != current_key:
            if write_game_process_event(self.settings, "end", self.active_game) is None:
                return current
            self.active_game = None
        if current and active_key != current_key:
            current["started_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            if write_game_process_event(self.settings, "start", current) is None:
                return current
            self.active_game = current
        return current

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._thread = threading.Thread(target=self._run, name="game-process-monitor", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.poll_once()
            except Exception:
                continue


def _event_duration_seconds(start_iso: str, end_iso: str) -> Optional[int]:
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
    except (TypeError, ValueError):
        return None
    delta = int((end - start).total_seconds())
    return delta if delta >= 0 else None


def _game_session_key(session: dict) -> tuple:
    return (
        str(session.get("played_at") or session.get("started_at") or ""),
        str(session.get("system_name") or session.get("system") or "").strip().lower(),
        str(session.get("game_name") or session.get("rom_name") or session.get("rom_path") or "").strip().lower(),
        str(session.get("rom_path") or "").strip().lower(),
    )


def _gameplay_session_key_text(session: dict) -> str:
    return json.dumps(_game_session_key(session), ensure_ascii=False, separators=(",", ":"))


def _gameplay_columns(session: dict) -> tuple[str, str, str, str]:
    return (
        str(session.get("played_at") or session.get("started_at") or ""),
        str(session.get("system_name") or session.get("system") or ""),
        str(session.get("game_name") or session.get("rom_name") or ""),
        str(session.get("rom_path") or ""),
    )


def _upsert_gameplay_session(connection, session: dict) -> None:
    session_key = _gameplay_session_key_text(session)
    existing = connection.execute(
        "SELECT payload FROM gameplay_history WHERE session_key = ?",
        (session_key,),
    ).fetchone()
    if existing:
        try:
            previous = json.loads(existing[0])
        except Exception:
            previous = {}
        if isinstance(previous, dict):
            session = {**previous, **session}
    played_at, system_name, game_name, rom_path = _gameplay_columns(session)
    updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    connection.execute(
        "INSERT INTO gameplay_history "
        "(session_key, played_at, system_name, game_name, rom_path, payload, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_key) DO UPDATE SET "
        "played_at=excluded.played_at, system_name=excluded.system_name, game_name=excluded.game_name, "
        "rom_path=excluded.rom_path, payload=excluded.payload, updated_at=excluded.updated_at",
        (
            session_key,
            played_at,
            system_name,
            game_name,
            rom_path,
            json.dumps(session, sort_keys=True, default=str),
            updated_at,
        ),
    )


def _ensure_gameplay_history_migrated(settings: Any) -> None:
    """Backfill the former app_state JSON list exactly once without deleting it."""
    path = database_path(settings.userdata_root)
    with open_database(path) as connection:
        marker = connection.execute(
            "SELECT 1 FROM app_state WHERE namespace = ? AND state_key = 'payload'",
            (GAMEPLAY_HISTORY_MIGRATION_NAMESPACE,),
        ).fetchone()
        if marker:
            return
        legacy = connection.execute(
            "SELECT payload FROM app_state WHERE namespace = ? AND state_key = 'payload'",
            (GAMEPLAY_HISTORY_NAMESPACE,),
        ).fetchone()
        try:
            rows = json.loads(legacy[0]) if legacy else []
        except Exception:
            rows = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                _upsert_gameplay_session(connection, row)
        connection.execute(
            "INSERT INTO app_state (namespace, state_key, payload, updated_at) VALUES (?, 'payload', 'true', ?) "
            "ON CONFLICT(namespace, state_key) DO UPDATE SET payload='true', updated_at=excluded.updated_at",
            (
                GAMEPLAY_HISTORY_MIGRATION_NAMESPACE,
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            ),
        )


def load_gameplay_history(settings: Any) -> List[dict]:
    """Load the Drone's durable, deduplicated completed gameplay history."""
    _ensure_gameplay_history_migrated(settings)
    with open_database(database_path(settings.userdata_root)) as connection:
        rows = connection.execute(
            "SELECT payload FROM gameplay_history ORDER BY played_at, session_key"
        ).fetchall()
    history = []
    for row in rows:
        try:
            payload = json.loads(row[0])
        except Exception:
            continue
        if isinstance(payload, dict):
            history.append(payload)
    return history


def load_gameplay_history_page(
    settings: Any,
    *,
    query: str = "",
    limit: int = 500,
    offset: int = 0,
) -> dict:
    """Return a gameplay-history page and total directly from SQLite."""
    _ensure_gameplay_history_migrated(settings)
    safe_limit = max(1, min(int(limit), 2000))
    safe_offset = max(0, int(offset))
    normalized_query = str(query or "").strip()
    parameters: list = []
    where = ""
    if normalized_query:
        escaped = normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where = (
            " WHERE system_name COLLATE NOCASE LIKE ? ESCAPE '\\' "
            "OR game_name COLLATE NOCASE LIKE ? ESCAPE '\\' "
            "OR rom_path COLLATE NOCASE LIKE ? ESCAPE '\\'"
        )
        parameters.extend([pattern, pattern, pattern])
    with open_database(database_path(settings.userdata_root)) as connection:
        total = int(connection.execute(f"SELECT COUNT(*) FROM gameplay_history{where}", parameters).fetchone()[0])
        rows = connection.execute(
            f"SELECT payload FROM gameplay_history{where} "
            "ORDER BY played_at DESC, session_key LIMIT ? OFFSET ?",
            [*parameters, safe_limit, safe_offset],
        ).fetchall()
    items = []
    for row in rows:
        try:
            payload = json.loads(row[0])
        except Exception:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return {"total": total, "limit": safe_limit, "offset": safe_offset, "items": items}


def _store_gameplay_history(settings: Any, sessions: List[dict]) -> None:
    _ensure_gameplay_history_migrated(settings)
    with open_database(database_path(settings.userdata_root)) as connection:
        for session in sessions:
            if isinstance(session, dict):
                _upsert_gameplay_session(connection, dict(session))


def pending_game_event_count(settings: Any) -> int:
    spool = _game_event_spool_dir(settings)
    try:
        return sum(1 for path in spool.iterdir() if path.is_file() and path.suffix == ".json")
    except OSError:
        return 0


def collect_game_event_sessions(
    settings: Any,
    repository: Optional[Any] = None,
    *,
    format_error: Optional[ErrorFormatter] = None,
) -> Tuple[List[dict], List[Path]]:
    """Drain process-monitor start/stop events into completed gameplay sessions."""
    formatter = format_error or (lambda error: str(error))
    spool = _game_event_spool_dir(settings)
    if not spool.exists() or not spool.is_dir():
        return [], []
    try:
        files = sorted(path for path in spool.iterdir() if path.is_file() and path.suffix == ".json")
    except OSError:
        return [], []

    collected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    active_sessions = load_payload(
        database_path(settings.userdata_root),
        "active_game_process_sessions",
        {},
    )
    if not isinstance(active_sessions, dict):
        active_sessions = {}
    sessions: List[dict] = []
    processed: List[Path] = []
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
            session = active_sessions.pop(resolved_rom, None)
            if not isinstance(session, dict):
                started_at = str(event.get("started_at") or "").strip()
                event_system = str(event.get("system_name") or "").strip()
                if started_at and event_system:
                    session = {
                        "played_at": started_at,
                        "system_name": event_system,
                        "game_name": Path(rom_value).name,
                        "rom_path": resolved_rom,
                    }
            if isinstance(session, dict):
                duration = _event_duration_seconds(str(session.get("played_at") or ""), played_at)
                if duration is not None:
                    session["duration_seconds"] = duration
                session["ended_at"] = played_at
                sessions.append(session)
            continue

        event_system = str(event.get("system_name") or "").strip()
        system_name = _system_from_launch_rom_path(settings, rom_path, event_system)
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
        active_sessions[resolved_rom] = session
    save_payload(database_path(settings.userdata_root), "active_game_process_sessions", active_sessions)
    if sessions:
        _store_gameplay_history(settings, sessions)
    return sessions, processed


def delete_game_event_spool(files: Optional[List[Path]]) -> None:
    """Remove spool files that have already been folded into gameplay history."""
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
    """Legacy parser for previously captured EmulationStation launch output."""
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
