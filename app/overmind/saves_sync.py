"""Local game-save scan hook.

Overmind no longer stores Drone save inventories. Keep this module as a stable
compatibility surface for callers/tests, but do not upload save rows or
thumbprints to Overmind.
"""

try:
    from ..common.logging_setup import _overmind_log
    from ..common.runtime_state import _SAVES_PUSH_REQUESTED
    from ..common.settings import Settings
    from ..storage import saves_store as _saves_store
    from ..transfer import local_network as _local_network
    from .overmind_client import _format_overmind_error
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.logging_setup import _overmind_log  # type: ignore
    from common.runtime_state import _SAVES_PUSH_REQUESTED  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage import saves_store as _saves_store  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from overmind.overmind_client import _format_overmind_error  # type: ignore


def _sync_saves_to_overmind(settings: Settings, base_url: str, token: str) -> dict:
    """Scan local saves only; Overmind save inventory upload is disabled."""
    if not _local_network.is_overmind_mode(settings):
        return {"status": "skipped", "reason": "local_network_mode"}
    try:
        summary = _saves_store.sync_saves_cache(settings.saves_root)
    except Exception as error:
        _overmind_log(f"Saves scan failed: error={_format_overmind_error(error)}")
        return {"status": "scan_failed"}
    _overmind_log(
        "Saves sync skipped: Overmind save inventory upload disabled "
        f"local_count={summary.get('count')} thumbprint={str(summary.get('thumbprint') or '')[:12]}"
    )
    _saves_store.clear_pending_changes(settings.saves_root)
    _SAVES_PUSH_REQUESTED.clear()
    return {"status": "disabled", "reason": "overmind_saves_disabled", "thumbprint": summary.get("thumbprint")}
