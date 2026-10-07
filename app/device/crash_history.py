"""Read and clear the crash history written by the Game crash notifier fix."""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from . import admin_fixes
    from . import notifications as _notifications
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from device import admin_fixes  # type: ignore
    from device import notifications as _notifications  # type: ignore

CURSOR_NAMESPACE = "crash_history_cursor.json"
# A crash this fresh at first startup is new, not pre-existing history.
FRESH_CRASH_GRACE_SECONDS = 60

MAX_RETURNED = 50
_TEXT_FIELDS = (
    "time", "game", "rom_path", "system", "emulator", "core", "reason", "action",
    "signature", "kernel_evidence", "batocera_version", "hostname", "log_excerpt",
)


def history_path(settings: Settings) -> Path:
    return settings.userdata_root / "system" / "game-crash-notifier" / "history.jsonl"


def _clean(record: Dict[str, Any]) -> Dict[str, Any]:
    cleaned: Dict[str, Any] = {key: str(record.get(key) or "") for key in _TEXT_FIELDS}
    for key in ("epoch", "duration_seconds", "joystick_count", "rom_size_bytes", "memory_available_mb"):
        value = record.get(key)
        cleaned[key] = value if isinstance(value, int) and not isinstance(value, bool) else None
    cleaned["rom_exists"] = record.get("rom_exists") if isinstance(record.get("rom_exists"), bool) else None
    cleaned["short_session"] = bool(record.get("short_session"))
    for key in ("joysticks", "toasts"):
        values = record.get(key)
        cleaned[key] = [str(value) for value in values] if isinstance(values, list) else []
    return cleaned


def list_crashes(settings: Settings, limit: int = MAX_RETURNED) -> Dict[str, Any]:
    """Return the newest crashes first, skipping any unreadable line."""
    crashes: List[Dict[str, Any]] = []
    try:
        lines = history_path(settings).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            crashes.append(_clean(record))
    crashes.sort(key=lambda row: row["epoch"] or 0, reverse=True)
    limit = max(1, min(int(limit), MAX_RETURNED))
    fix = admin_fixes.get_fix(settings, admin_fixes.CRASH_NOTIFIER_ID)
    return {
        "crashes": crashes[:limit],
        "total": len(crashes),
        "fix_enabled": bool(fix.get("enabled")),
    }


def clear_crashes(settings: Settings) -> int:
    """Delete the history file and return how many entries were removed."""
    removed = list_crashes(settings)["total"]
    try:
        history_path(settings).unlink()
    except FileNotFoundError:
        pass
    return removed


def format_crash_report(record: Dict[str, Any]) -> str:
    """Plain-text report with the same information the Game Crashes page shows."""
    crash = _clean(record)
    rom_state = ""
    if crash["rom_exists"] is False:
        rom_state = "file not found"
    elif crash["rom_size_bytes"] is not None:
        rom_state = f"{crash['rom_size_bytes']:,} bytes"
    controllers = ""
    if crash["joystick_count"] is not None:
        controllers = str(crash["joystick_count"])
        if crash["joysticks"]:
            controllers += f" ({', '.join(crash['joysticks'])})"
    duration = ""
    if crash["duration_seconds"] is not None:
        duration = f"{crash['duration_seconds']}s" + (" (very short)" if crash["short_session"] else "")
    memory = f"{crash['memory_available_mb']} MB" if crash["memory_available_mb"] is not None else ""
    device = crash["hostname"]
    device_id = str(record.get("device_id") or "")
    if device_id:
        device = f"{device} ({device_id})" if device else device_id
    fields = (
        ("Device", device),
        ("When", crash["time"]),
        ("Game", crash["game"]),
        ("System", crash["system"]),
        ("What happened", crash["reason"]),
        ("What to try", crash["action"]),
        ("ROM file", crash["rom_path"]),
        ("ROM size / state", rom_state),
        ("Emulator / core", " / ".join(part for part in (crash["emulator"], crash["core"]) if part)),
        ("Detected as", crash["signature"]),
        ("Session length", duration),
        ("Controllers connected", controllers),
        ("Free memory", memory),
        ("Batocera version", crash["batocera_version"]),
        ("Kernel log", crash["kernel_evidence"]),
    )
    lines = [f"{label}: {value}" for label, value in fields if value]
    lines += ["", "Launch log around the failure:", crash["log_excerpt"] or "(no log text was captured)"]
    return "\n".join(lines)


def _history_records(settings: Settings) -> List[Dict[str, Any]]:
    try:
        lines = history_path(settings).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("epoch"), int):
            records.append(record)
    records.sort(key=lambda row: row["epoch"])
    return records


def ingest_new_crashes(settings: Settings, now: Optional[float] = None) -> int:
    """Record a ``game_crash`` event for each crash the hook wrote since the last pass.

    The hook runs outside the Drone, so this bridges its history file into the
    notifications inbox and email pipeline.  The first pass never replays older
    history; it only skips crashes that predate this Drone's start by more than
    the grace window.  Returns how many events were recorded.
    """
    now = time.time() if now is None else now
    database = _state_database_path(settings.userdata_root)
    records = _history_records(settings)
    stored = _load_state_payload(database, CURSOR_NAMESPACE, {})
    cursor = stored.get("epoch") if isinstance(stored, dict) and isinstance(stored.get("epoch"), int) else None
    if cursor is None:
        cursor = max([row["epoch"] for row in records if row["epoch"] < now - FRESH_CRASH_GRACE_SECONDS] or [0])
        _save_state_payload(database, CURSOR_NAMESPACE, {"epoch": cursor})
    recorded = 0
    for record in records:
        if record["epoch"] <= cursor:
            continue
        crash = _clean(record)
        details = dict(record)
        details["device_id"] = settings.device_id
        _notifications.record_event(
            settings,
            "game_crash",
            f"{crash['game'] or 'A game'} ({crash['system'] or 'unknown system'}) crashed",
            message=f"{crash['reason']}. {crash['action']}".strip(),
            details=details,
        )
        # Advance one record at a time so a failure never replays earlier ones.
        cursor = record["epoch"]
        _save_state_payload(database, CURSOR_NAMESPACE, {"epoch": cursor})
        recorded += 1
    return recorded
