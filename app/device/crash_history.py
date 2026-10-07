"""Read and clear the crash history written by the Game crash notifier fix."""

import json
from pathlib import Path
from typing import Any, Dict, List

try:
    from ..common.settings import Settings
    from . import admin_fixes
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from device import admin_fixes  # type: ignore

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
