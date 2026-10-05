"""Structured one-line Stream Deck event logging.

In the Drone process events go to the existing activity log (``_drone_log`` ->
``drone.log``, the "drone_activity" Log Source). The isolated worker swaps the
sink for ``print`` because its stdout is the integration's own size-rotating
``logs/runtime.log`` (see ``worker.configure_worker_logging``).

Values are JSON-quoted when they contain whitespace so a crafted ROM name or
script name can never forge an extra log line. Callers must not pass script
source code; only IDs/names are logged.
"""

import json
from typing import Any, Callable, Optional

try:
    from ...common.logging_setup import _drone_log
except ImportError:  # pragma: no cover - flat execution
    from common.logging_setup import _drone_log  # type: ignore


_MAX_VALUE_CHARS = 300
_SINK: Optional[Callable[[str], None]] = None
_LIFECYCLE_EVENTS = {
    "integration-enabled", "integration-disabled", "integration-enable-failed",
    "integration-removed", "integration-repaired", "runtime-started", "runtime-stopped",
}


def set_log_sink(sink: Optional[Callable[[str], None]]) -> None:
    global _SINK
    _SINK = sink


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(round(value, 3)) if isinstance(value, float) else str(value)
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=True, default=str, separators=(",", ":"))
    else:
        text = str(value)
    if len(text) > _MAX_VALUE_CHARS:
        text = text[: _MAX_VALUE_CHARS - 3] + "..."
    if not text or any(ch.isspace() or ch in "\"=\\" for ch in text) or not text.isprintable():
        return json.dumps(text, ensure_ascii=True)
    return text


def format_event(event: str, **fields: Any) -> str:
    parts = [f"[streamdeck] event={_format_value(event)}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={_format_value(value)}")
    return " ".join(parts)


def log_event(event: str, **fields: Any) -> str:
    line = format_event(event, **fields)
    sink = _SINK
    try:
        if sink is not None:
            sink(line)
        else:
            _drone_log(line, also_stdout=event in _LIFECYCLE_EVENTS)
    except Exception:  # noqa: BLE001 - logging must never break an action
        pass
    return line
