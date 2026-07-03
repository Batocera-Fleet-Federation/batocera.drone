"""Filesystem change monitoring for Drone telemetry reported to Overmind."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


def _watch_roots(settings: Any) -> List[Path]:
    return [
        settings.roms_root,
        settings.userdata_root / "system" / "configs",
        settings.userdata_root / "system" / "logs",
        settings.log_dir,
    ]


def filesystem_snapshot(settings: Any, max_files: int = 5000) -> Dict[str, dict]:
    """Capture watched file sizes and modification times."""
    snapshot: Dict[str, dict] = {}
    checked = 0
    for root in _watch_roots(settings):
        if not root.exists():
            continue
        try:
            for path in root.rglob("*"):
                checked += 1
                if checked > max_files:
                    return snapshot
                if not path.is_file():
                    continue
                stat = path.stat()
                snapshot[str(path.resolve())] = {"size": stat.st_size, "mtime": int(stat.st_mtime)}
        except Exception:
            continue
    return snapshot


def filesystem_events(
    settings: Any,
    previous: Dict[str, dict],
    current: Dict[str, dict],
    event_type: str = "filesystem_event",
) -> List[dict]:
    """Build capped create, update, and delete events between snapshots."""
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    events = []
    for path, meta in current.items():
        old = previous.get(path)
        if not old:
            action = "create"
        elif old != meta:
            action = "update"
        else:
            continue
        events.append({
            "drone_id": settings.overmind_device_id,
            "event_type": event_type,
            "timestamp": now,
            "path": path,
            "metadata": {"action": action, **meta, "old": old},
        })
    for path, old in previous.items():
        if path not in current:
            events.append({
                "drone_id": settings.overmind_device_id,
                "event_type": event_type,
                "timestamp": now,
                "path": path,
                "metadata": {"action": "delete", "old": old},
            })
    return events[:100]
