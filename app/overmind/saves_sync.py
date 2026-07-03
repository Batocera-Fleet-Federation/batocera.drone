"""Game-saves sync to Overmind.

Extracted from ``drone_api.py``. Lists local game saves, uploads them to Overmind (with
status handling + the shared saves thumbprint in the ROM-metadata cache state), and
clears the ``_SAVES_PUSH_REQUESTED`` resync flag once the current thumbprint is reported.
"""

import os
from urllib.parse import quote

try:
    from ..common.logging_setup import _overmind_log
    from ..common.runtime_state import _SAVES_PUSH_REQUESTED
    from ..common.settings import Settings
    from ..storage import saves_store as _saves_store
    from ..storage.rom_metadata_store import _read_rom_metadata_cache_state, _update_rom_metadata_cache_state
    from ..transfer import local_network as _local_network
    from .overmind_client import _format_overmind_error, _overmind_post_json_with_status
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.logging_setup import _overmind_log  # type: ignore
    from common.runtime_state import _SAVES_PUSH_REQUESTED  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage import saves_store as _saves_store  # type: ignore
    from storage.rom_metadata_store import _read_rom_metadata_cache_state, _update_rom_metadata_cache_state  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from overmind.overmind_client import _format_overmind_error, _overmind_post_json_with_status  # type: ignore

# Local copy (drone_api keeps its own for the rom-metadata sync path still resident there);
# both read the same env var, so the value matches.
OVERMIND_UPLOAD_TIMEOUT_SECONDS = max(10, int(os.environ.get("OVERMIND_UPLOAD_TIMEOUT_SECONDS", "60")))


def _sync_saves_to_overmind(settings: Settings, base_url: str, token: str) -> dict:
    """Scan game saves and push created/updated/deleted entries to Overmind.

    Runs on the ROM-metadata poll cadence but with its own small payload so a saves
    change never forces a (much larger) ROM/BIOS re-upload. Uses a single-shot
    ``inventory_delta`` so it upserts changed saves and removes deleted ones WITHOUT
    touching ROM/BIOS rows (``replace_all`` would clear those). The on-disk saves
    thumbprint is persisted every scan so heartbeats can report and compare it; the
    upload only fires when something actually changed or a heartbeat flagged drift.
    """
    if not _local_network.is_overmind_mode(settings):
        return {"status": "skipped", "reason": "local_network_mode"}
    try:
        summary = _saves_store.sync_saves_cache(settings.saves_root)
    except Exception as error:
        _overmind_log(f"Saves scan failed: error={_format_overmind_error(error)}")
        return {"status": "scan_failed"}
    current = str(summary.get("thumbprint") or "").strip()
    _update_rom_metadata_cache_state(settings, saves_files_thumbprint=current)
    pending = _saves_store.read_pending_changes(settings.saves_root)
    has_changes = bool(pending.get("saves") or pending.get("deleted"))
    push_requested = _SAVES_PUSH_REQUESTED.is_set()
    try:
        uploaded_state = _read_rom_metadata_cache_state(settings, "saves_files_thumbprint_uploaded")
    except Exception:
        uploaded_state = {}
    last_uploaded = str(uploaded_state.get("saves_files_thumbprint_uploaded") or "").strip()
    # Explain why a saves sync did or did not fire (mirrors the asset sync trigger log).
    saves_reasons = []
    if has_changes:
        saves_reasons.append(f"changed_saves={len(pending.get('saves') or [])}")
        if pending.get("deleted"):
            saves_reasons.append(f"deleted_saves={len(pending.get('deleted') or [])}")
    if push_requested:
        saves_reasons.append("heartbeat_thumbprint_mismatch")
    if not has_changes and not push_requested and current != last_uploaded:
        saves_reasons.append("thumbprint_changed_since_upload")
    will_upload = bool(has_changes or push_requested or current != last_uploaded)
    _overmind_log(
        f"Saves sync trigger: will_upload={will_upload} reasons={','.join(saves_reasons) or 'none'} "
        f"thumbprint={current[:12]} last_uploaded={last_uploaded[:12]} "
        f"has_changes={has_changes} push_requested={push_requested}"
    )
    if not has_changes and not push_requested and current == last_uploaded:
        return {"status": "unchanged", "thumbprint": current}
    if has_changes:
        upsert_rows = pending.get("saves") or []
        deleted_rows = pending.get("deleted") or []
    else:
        # Drift resync / first push with an empty change queue: re-assert the full set.
        upsert_rows = _saves_store.list_saves(settings.saves_root)
        deleted_rows = []
    payload = {
        "device_id": settings.overmind_device_id,
        "type": "asset_metadata",
        "update_mode": "inventory_delta",
        "saves": upsert_rows,
        "deleted": {"saves": deleted_rows},
        "saves_files_thumbprint": current,
        "saves_root": str(settings.saves_root),
        "inventory_complete": True,
    }
    device_id = quote(settings.overmind_device_id, safe="")
    upload_url = f"{base_url}/api/devices/{device_id}/rom-metadata"
    try:
        status_code, _ = _overmind_post_json_with_status(
            upload_url,
            payload,
            token=token,
            settings=settings,
            timeout_seconds=OVERMIND_UPLOAD_TIMEOUT_SECONDS,
        )
    except Exception as error:
        _overmind_log(f"Saves upload failed: error={_format_overmind_error(error)}")
        return {"status": "upload_failed"}
    _saves_store.clear_pending_changes(settings.saves_root)
    _update_rom_metadata_cache_state(settings, saves_files_thumbprint_uploaded=current)
    _SAVES_PUSH_REQUESTED.clear()
    # High-level lifecycle event -> stdout (also recorded in overmind.log).
    _overmind_log(
        f"Saves sync uploaded: upserts={len(upsert_rows)} deletes={len(deleted_rows)} "
        f"thumbprint={current[:12]} status={status_code}",
        also_stdout=True,
    )
    return {"status": "ok", "upserts": len(upsert_rows), "deletes": len(deleted_rows), "thumbprint": current}
