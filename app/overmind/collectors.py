"""Collectors that assemble Overmind heartbeat sub-payloads.

Extracted from ``drone_api.py``. Thin orchestration over the extracted
``overmind_*`` modules: filesystem create/delete events and the game-log payload
(gameplay sessions + log sources) the drone ships to Overmind.
"""

from typing import Dict, List, Optional

try:
    from ..common.settings import Settings
    from .overmind_client import _format_overmind_error
    from .overmind_filesystem import filesystem_events as _build_filesystem_events
    from .overmind_game_logs import collect_game_logs as _build_game_log_payload
    from .overmind_reporting import collect_log_sources as _collect_log_sources
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from overmind.overmind_client import _format_overmind_error  # type: ignore
    from overmind.overmind_filesystem import filesystem_events as _build_filesystem_events  # type: ignore
    from overmind.overmind_game_logs import collect_game_logs as _build_game_log_payload  # type: ignore
    from overmind.overmind_reporting import collect_log_sources as _collect_log_sources  # type: ignore


OVERMIND_EVENT_TYPES = {
    "gameplay": "gameplay_activity",
    "rom_update": "rom_update",
    "filesystem": "filesystem_event",
    "speed": "speed_sample",
    "peer": "peer_health",
}


def _filesystem_events(settings: Settings, previous: Dict[str, dict], current: Dict[str, dict]) -> List[dict]:
    return _build_filesystem_events(
        settings,
        previous,
        current,
        event_type=OVERMIND_EVENT_TYPES["filesystem"],
    )


def _collect_game_logs(settings: Settings, repository: Optional["RomRepository"] = None, log_data: Optional[dict] = None) -> dict:
    return _build_game_log_payload(
        settings,
        repository,
        log_data,
        collect_log_sources=_collect_log_sources,
        format_error=_format_overmind_error,
    )

