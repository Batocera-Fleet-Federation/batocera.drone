"""ROM-metadata sync to Overmind (the poll -> upload orchestration).

Extracted from ``drone_api.py``. ``_poll_rom_metadata_once`` runs one scan+hash+sync
cycle; ``_sync_rom_metadata_to_overmind(_locked)`` uploads the inventory/delta (chunked,
token-reclaim on 401) and marks the cache clean; ``_complete``/``_defer`` finalize or
postpone an upload. All take the ``RomRepository`` as a parameter (query object); the
poller *threads* that call these stay in ``drone_api`` (they own the ``*_STARTED`` flags).
"""

import os
import sys
import time
from typing import Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import quote

try:
    from ..common.logging_setup import _overmind_log
    from ..common.runtime_state import _ASSET_PUSH_REQUESTED
    from ..common.settings import Settings
    from ..roms.rom_inventory import (
        BIOS_INVENTORY_FINGERPRINT_ALGORITHM,
        ROM_INVENTORY_FINGERPRINT_ALGORITHM,
        _chunk_rom_metadata_delta,
        _chunk_rom_metadata_inventory,
        _json_payload_size_bytes,
        _rom_inventory_fingerprint_from_cache_state,
    )
    from ..roms.rom_metadata_state import (
        _begin_rom_metadata_activity,
        _build_rom_metadata_snapshot_from_cache,
        _end_rom_metadata_activity,
        _mark_rom_metadata_upload_clean,
    )
    from ..roms.rom_scanner import _hash_rom_metadata_batches, _poll_rom_metadata_cache
    from ..storage import saves_store as _saves_store
    from ..storage.rom_metadata_store import (
        _clear_pending_rom_metadata_changes,
        _load_rom_metadata_cache,
        _persist_rom_metadata_cache,
        _read_pending_rom_metadata_changes,
        _update_rom_metadata_cache_state,
    )
    from ..transfer import local_network as _local_network
    from .heartbeat_sync import _local_asset_thumbprints
    from .overmind_client import _format_overmind_error, _overmind_post_json_with_status
    from .overmind_config import _load_overmind_config_for_settings
    from .registration import _reclaim_overmind_token_after_unauthorized, _register_or_claim_overmind_token
    from .saves_sync import _sync_saves_to_overmind
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.logging_setup import _overmind_log  # type: ignore
    from common.runtime_state import _ASSET_PUSH_REQUESTED  # type: ignore
    from common.settings import Settings  # type: ignore
    from roms.rom_inventory import (  # type: ignore
        BIOS_INVENTORY_FINGERPRINT_ALGORITHM,
        ROM_INVENTORY_FINGERPRINT_ALGORITHM,
        _chunk_rom_metadata_delta,
        _chunk_rom_metadata_inventory,
        _json_payload_size_bytes,
        _rom_inventory_fingerprint_from_cache_state,
    )
    from roms.rom_metadata_state import (  # type: ignore
        _begin_rom_metadata_activity,
        _build_rom_metadata_snapshot_from_cache,
        _end_rom_metadata_activity,
        _mark_rom_metadata_upload_clean,
    )
    from roms.rom_scanner import _hash_rom_metadata_batches, _poll_rom_metadata_cache  # type: ignore
    from storage import saves_store as _saves_store  # type: ignore
    from storage.rom_metadata_store import (  # type: ignore
        _clear_pending_rom_metadata_changes,
        _load_rom_metadata_cache,
        _persist_rom_metadata_cache,
        _read_pending_rom_metadata_changes,
        _update_rom_metadata_cache_state,
    )
    from transfer import local_network as _local_network  # type: ignore
    from overmind.heartbeat_sync import _local_asset_thumbprints  # type: ignore
    from overmind.overmind_client import _format_overmind_error, _overmind_post_json_with_status  # type: ignore
    from overmind.overmind_config import _load_overmind_config_for_settings  # type: ignore
    from overmind.registration import _reclaim_overmind_token_after_unauthorized, _register_or_claim_overmind_token  # type: ignore
    from overmind.saves_sync import _sync_saves_to_overmind  # type: ignore

# Local copy (drone_api keeps its own; both read the same env). Not test-patched.
OVERMIND_UPLOAD_TIMEOUT_SECONDS = max(10, int(os.environ.get("OVERMIND_UPLOAD_TIMEOUT_SECONDS", "60")))


def _sync_rom_metadata_to_overmind(
    settings: Settings,
    repository: "RomRepository",
    config: dict,
    base_url: str,
    token: str,
    prepared_poll: Optional[Tuple[dict, bool, dict]] = None,
    *,
    force_upload: bool = False,
) -> dict:
    if not _local_network.is_overmind_mode(settings):
        return {"status": "skipped", "reason": "local_network_mode", "changed": False, "uploads": []}
    if not _begin_rom_metadata_activity("sync"):
        cache, _ = _load_rom_metadata_cache(settings)
        snapshot = _build_rom_metadata_snapshot_from_cache(settings, cache)
        return {
            "status": "skipped",
            "reason": "metadata_already_running",
            "changed": False,
            "rom_count": len(snapshot.get("roms") or []),
            "bios_count": len(snapshot.get("bios") or []),
            "artwork_count": len(snapshot.get("artwork") or []),
            "uploads": [],
            "stats": {"metadata_already_running": True},
        }
    try:
        return _sync_rom_metadata_to_overmind_locked(
            settings,
            repository,
            config,
            base_url,
            token,
            prepared_poll=prepared_poll,
            force_upload=force_upload,
        )
    finally:
        _end_rom_metadata_activity()


def _sync_rom_metadata_to_overmind_locked(
    settings: Settings,
    repository: "RomRepository",
    config: dict,
    base_url: str,
    token: str,
    prepared_poll: Optional[Tuple[dict, bool, dict]] = None,
    *,
    force_upload: bool = False,
) -> dict:
    # ROM_METADATA_FINGERPRINT_BATCH_SIZE stays single-source in drone_api (tests patch it); lazy-import.
    try:
        from ..drone_api import ROM_METADATA_FINGERPRINT_BATCH_SIZE
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import ROM_METADATA_FINGERPRINT_BATCH_SIZE  # type: ignore
    poll_started = time.monotonic()
    snapshot, changed, stats = prepared_poll or _poll_rom_metadata_cache(settings, repository)
    rom_count = len(snapshot.get("roms") or [])
    bios_count = len(snapshot.get("bios") or [])
    artwork_count = len(snapshot.get("artwork") or [])
    device_id = quote(settings.overmind_device_id, safe="")
    upload_url = f"{base_url}/api/devices/{device_id}/rom-metadata"
    uploads = []

    def upload(payload: dict, phase: str) -> dict:
        nonlocal token
        update_mode = str(payload.get("update_mode") or phase)
        payload_bytes = _json_payload_size_bytes(payload)
        chunk_label = ""
        if update_mode == "inventory_chunk":
            chunk_label = f" chunk={int(payload.get('chunk_index') or 0) + 1}/{payload.get('chunk_total')}"
        try:
            status_code, response = _overmind_post_json_with_status(
                upload_url,
                payload,
                token=token,
                settings=settings,
                timeout_seconds=OVERMIND_UPLOAD_TIMEOUT_SECONDS,
            )
        except HTTPError as error:
            if error.code != 401:
                print(
                    f"Asset metadata upload failed: phase={phase} mode={update_mode}{chunk_label} payload_bytes={payload_bytes} error={_format_overmind_error(error)}",
                    file=sys.stderr,
                    flush=True,
                )
                raise
            replacement_token = _reclaim_overmind_token_after_unauthorized(settings, repository, config, base_url, error)
            if not replacement_token:
                print(
                    f"Asset metadata upload failed: phase={phase} mode={update_mode}{chunk_label} payload_bytes={payload_bytes} error={_format_overmind_error(error)}",
                    file=sys.stderr,
                    flush=True,
                )
                raise
            token = replacement_token
            try:
                status_code, response = _overmind_post_json_with_status(
                    upload_url,
                    payload,
                    token=token,
                    settings=settings,
                    timeout_seconds=OVERMIND_UPLOAD_TIMEOUT_SECONDS,
                )
            except Exception as retry_error:
                print(
                    f"Asset metadata upload failed after token refresh: phase={phase} mode={update_mode}{chunk_label} payload_bytes={payload_bytes} error={_format_overmind_error(retry_error)}",
                    file=sys.stderr,
                    flush=True,
                )
                raise
        except Exception as error:
            print(
                f"Asset metadata upload failed: phase={phase} mode={update_mode}{chunk_label} payload_bytes={payload_bytes} error={_format_overmind_error(error)}",
                file=sys.stderr,
                flush=True,
            )
            raise
        uploads.append({"phase": phase, "status_code": status_code, "payload_bytes": payload_bytes, "response": response})
        return response

    pending_changes = _read_pending_rom_metadata_changes(settings)
    has_cached_assets = bool(rom_count or bios_count or artwork_count)
    # A heartbeat that reported a thumbprint differing from ours means Overmind's
    # stored asset set drifted from the Drone's; resync by pushing a full inventory.
    push_requested = _ASSET_PUSH_REQUESTED.is_set()
    full_refresh = bool(
        force_upload
        or push_requested
        or stats.get("full_refresh_pending")
        or (has_cached_assets and not stats.get("had_successful_upload"))
    )
    payloads = _chunk_rom_metadata_inventory(settings, snapshot, replace_all=True) if full_refresh else _chunk_rom_metadata_delta(settings, snapshot, pending_changes)
    should_upload_inventory = bool(payloads)

    # Explain exactly why this sync did or did not fire. The reasons map 1:1 to the
    # full_refresh / delta decision inputs above, so "syncing when nothing changed" can be
    # traced to its trigger (e.g. heartbeat_thumbprint_mismatch sets _ASSET_PUSH_REQUESTED,
    # or a non-empty pending change queue from the scan).
    _pending_deleted = pending_changes.get("deleted") if isinstance(pending_changes.get("deleted"), dict) else {}
    pending_roms = len(pending_changes.get("roms") or [])
    pending_bios = len(pending_changes.get("bios") or [])
    pending_artwork = len(pending_changes.get("artwork") or [])
    pending_deleted = len(_pending_deleted.get("roms") or []) + len(_pending_deleted.get("bios") or []) + len(_pending_deleted.get("artwork") or [])
    first_upload_after_cache = bool(has_cached_assets and not stats.get("had_successful_upload"))
    sync_reasons = []
    if force_upload:
        sync_reasons.append("force_upload")
    if push_requested:
        sync_reasons.append("heartbeat_thumbprint_mismatch")
    if stats.get("full_refresh_pending"):
        sync_reasons.append("full_refresh_pending")
    if first_upload_after_cache:
        sync_reasons.append("first_upload_after_cache_load")
    if not full_refresh and should_upload_inventory:
        if pending_roms:
            sync_reasons.append(f"delta_roms={pending_roms}")
        if pending_bios:
            sync_reasons.append(f"delta_bios={pending_bios}")
        if pending_artwork:
            sync_reasons.append(f"delta_artwork={pending_artwork}")
        if pending_deleted:
            sync_reasons.append(f"delta_deletes={pending_deleted}")
    if not should_upload_inventory:
        sync_reasons.append("no_changes")
    decision = "full_refresh" if full_refresh else ("delta" if should_upload_inventory else "skip")
    _overmind_log(
        f"Asset metadata sync trigger: decision={decision} will_upload={should_upload_inventory} "
        f"reasons={','.join(sync_reasons) or 'none'} pending_roms={pending_roms} pending_bios={pending_bios} "
        f"pending_artwork={pending_artwork} pending_deletes={pending_deleted} chunks={len(payloads)} "
        f"force_upload={force_upload} push_requested={push_requested} "
        f"full_refresh_pending={bool(stats.get('full_refresh_pending'))} first_upload={first_upload_after_cache}"
    )

    if should_upload_inventory:
        upload_kind = "full refresh" if full_refresh else "delta"
        payload_sizes = [_json_payload_size_bytes(payload) for payload in payloads]
        total_payload_bytes = sum(payload_sizes)
        max_payload_bytes = max(payload_sizes) if payload_sizes else 0
        # High-level lifecycle event -> stdout (also recorded in overmind.log).
        _overmind_log(
            f"Asset metadata {upload_kind} sync started: roms={rom_count} bios={bios_count} artwork={artwork_count} chunks={len(payloads)} total_payload_bytes={total_payload_bytes}",
            also_stdout=True,
        )
        # Per-chunk detail -> overmind.log only.
        _overmind_log(
            f"Asset metadata {upload_kind} sync detail: endpoint={upload_url} max_payload_bytes={max_payload_bytes} timeout_seconds={OVERMIND_UPLOAD_TIMEOUT_SECONDS} force={force_upload}"
        )
        accepted_roms = 0
        accepted_bios = 0
        accepted_artwork = 0
        for index, payload in enumerate(payloads, start=1):
            payload_bytes = _json_payload_size_bytes(payload)
            _overmind_log(
                f"Asset metadata inventory chunk upload started: chunk={index}/{len(payloads)} payload_bytes={payload_bytes} roms={len(payload.get('roms') or [])} bios={len(payload.get('bios') or [])} artwork={len(payload.get('artwork') or [])}"
            )
            response = upload(payload, "inventory")
            accepted_roms += int(response.get("rom_count") or 0)
            accepted_bios += int(response.get("bios_count") or 0)
            accepted_artwork += int(response.get("artwork_count") or 0)
            _overmind_log(
                f"Asset metadata inventory chunk upload succeeded: chunk={index}/{len(payloads)} payload_bytes={payload_bytes} accepted_roms={response.get('rom_count')} accepted_bios={response.get('bios_count')} accepted_artwork={response.get('artwork_count')}"
            )
        _mark_rom_metadata_upload_clean(
            settings,
            snapshot.get("romset_files_thumbprint") or snapshot.get("rom_inventory_fingerprint"),
            snapshot.get("bios_files_thumbprint"),
        )
        _overmind_log(
            f"Asset metadata {upload_kind} sync succeeded: accepted_roms={accepted_roms} accepted_bios={accepted_bios} accepted_artwork={accepted_artwork}"
        )
        # Flush the pending-change queue now that this snapshot's inventory was accepted.
        # Without this the same pending rows re-upload on every poll forever (observed as
        # a steady "decision=delta delta_roms=N" loop in overmind.log).
        _clear_pending_rom_metadata_changes(settings)

    hash_batches = 0
    hashed_roms = 0
    hash_patch_failed = False
    for patch in _hash_rom_metadata_batches(settings, repository, batch_size=ROM_METADATA_FINGERPRINT_BATCH_SIZE):
        patch_roms = patch.get("roms") if isinstance(patch.get("roms"), list) else []
        payload = {"device_id": settings.overmind_device_id, **patch}
        progress = patch.get("hash_progress") or {}
        _overmind_log(
            f"Asset metadata hash patch sync started: endpoint={upload_url} payload_bytes={_json_payload_size_bytes(payload)} batch_roms={len(patch_roms)} processed={progress.get('processed')}/{progress.get('total')}"
        )
        try:
            upload(payload, "rom_hash_patch")
        except Exception:
            # fingerprint is already persisted to the local cache before the patch is sent,
            # so without this the next poll sees nothing pending to hash and never
            # resends — leaving Overmind with fingerprint-less rows forever. Flag a full
            # refresh so the next poll re-uploads the inventory (now carrying fingerprint)
            # and Overmind converges.
            hash_patch_failed = True
            _update_rom_metadata_cache_state(settings, dirty=True, full_refresh_pending=True)
            _overmind_log(
                "Asset metadata hash patch upload failed; flagged full refresh so fingerprint values resync on the next poll"
            )
            break
        hash_batches += 1
        hashed_roms += len(patch_roms)
    if not hash_patch_failed and (should_upload_inventory or hash_batches):
        if hash_batches:
            cache, _ = _load_rom_metadata_cache(settings)
            snapshot = _build_rom_metadata_snapshot_from_cache(settings, cache)
        _mark_rom_metadata_upload_clean(
            settings,
            snapshot.get("romset_files_thumbprint") or snapshot.get("rom_inventory_fingerprint"),
            snapshot.get("bios_files_thumbprint"),
        )

    if not should_upload_inventory and not hash_batches:
        # Advertise the fingerprint of what the drone *actually* holds (fingerprint
        # included), even when there is nothing to upload. If a past hash patch
        # never reached Overmind, its stored fingerprint still reflects the
        # fingerprint-less inventory; reporting the true data fingerprint in the next
        # heartbeat lets Overmind detect the drift and queue a resync on its own,
        # so already-stuck drones recover without a manual force.
        current_fingerprint = snapshot.get("rom_inventory_fingerprint")
        current_bios_thumbprint = str(snapshot.get("bios_files_thumbprint") or "")
        stored_romset, stored_bios = _local_asset_thumbprints(settings)
        stored_legacy = _rom_inventory_fingerprint_from_cache_state(settings) or ""
        if current_fingerprint and (
            current_fingerprint != stored_romset
            or current_fingerprint != stored_legacy
            or current_bios_thumbprint != stored_bios
        ):
            _update_rom_metadata_cache_state(
                settings,
                rom_inventory_fingerprint=current_fingerprint,
                rom_inventory_fingerprint_algorithm=ROM_INVENTORY_FINGERPRINT_ALGORITHM,
                romset_files_thumbprint=current_fingerprint,
                bios_files_thumbprint=current_bios_thumbprint,
                bios_inventory_fingerprint_algorithm=BIOS_INVENTORY_FINGERPRINT_ALGORITHM,
            )
        _overmind_log(
            f"Asset metadata sync skipped: no changes detected systems={stats.get('systems_scanned')} roms={stats.get('roms_discovered')} bios={stats.get('bios_discovered')} artwork={stats.get('artwork_discovered')} duration_ms={int((time.monotonic() - poll_started) * 1000)}"
        )
        return {
            "status": "skipped",
            "reason": "no_changes",
            "rom_count": rom_count,
            "bios_count": bios_count,
            "artwork_count": artwork_count,
            "changed": changed,
            "stats": stats,
        }
    _overmind_log(
        f"Asset metadata sync finished: inventory_uploaded={should_upload_inventory} hash_batches={hash_batches} hashed_roms={hashed_roms} duration_ms={int((time.monotonic() - poll_started) * 1000)}",
        also_stdout=bool(should_upload_inventory or hash_batches),
    )
    return {
        "status": "uploaded",
        "uploads": uploads,
        "rom_count": rom_count,
        "bios_count": bios_count,
        "artwork_count": artwork_count,
        "hash_batches": hash_batches,
        "hashed_roms": hashed_roms,
        "changed": changed,
        "forced": force_upload,
        "stats": stats,
    }


def _complete_local_rom_metadata_cache(settings: Settings, repository: "RomRepository", reason: str) -> dict:
    # ROM_METADATA_FINGERPRINT_BATCH_SIZE stays single-source in drone_api (tests patch it); lazy-import.
    try:
        from ..drone_api import ROM_METADATA_FINGERPRINT_BATCH_SIZE
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import ROM_METADATA_FINGERPRINT_BATCH_SIZE  # type: ignore
    hash_batches = 0
    hashed_roms = 0
    for patch in _hash_rom_metadata_batches(settings, repository, batch_size=ROM_METADATA_FINGERPRINT_BATCH_SIZE):
        hash_batches += 1
        hashed_roms += len(patch.get("roms") or [])
    print(
        f"Asset metadata cached locally: upload_deferred={reason} hash_batches={hash_batches} hashed_roms={hashed_roms}",
        file=sys.stdout,
        flush=True,
    )
    return {
        "status": "cached",
        "reason": reason,
        "hash_batches": hash_batches,
        "hashed_roms": hashed_roms,
    }


def _defer_rom_metadata_upload(settings: Settings, reason: str) -> dict:
    cache, _ = _load_rom_metadata_cache(settings)
    cache["dirty"] = True
    _persist_rom_metadata_cache(settings, cache)
    print(
        f"Asset metadata upload deferred: reason={reason}",
        file=sys.stderr,
        flush=True,
    )
    return {
        "status": "deferred",
        "reason": reason,
        "changed": False,
    }


# _sync_saves_to_overmind now lives in overmind/saves_sync.py (re-exported below).


def _poll_rom_metadata_once(settings: Settings, repository: "RomRepository") -> dict:
    if not _begin_rom_metadata_activity("poll"):
        return {"status": "skipped", "reason": "metadata_already_running", "changed": False}
    try:
        prepared_poll = _poll_rom_metadata_cache(settings, repository)
        if _local_network.is_local_mode(settings) and not _local_network.is_overmind_mode(settings):
            try:
                _saves_store.sync_saves_cache(settings.saves_root)
            except Exception as error:
                print(f"Local saves cache scan failed: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
            return _complete_local_rom_metadata_cache(settings, repository, "overmind_disabled")
        config = _load_overmind_config_for_settings(settings)
        base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
        token = str(config.get("overmind_token") or "").strip()
        if not base_url:
            return _complete_local_rom_metadata_cache(settings, repository, "overmind_not_configured")
        if not token:
            auth_token = str(config.get("overmind_auth_token") or "").strip()
            if auth_token:
                token = _register_or_claim_overmind_token(settings, repository, config, base_url) or ""
            if not token:
                return _complete_local_rom_metadata_cache(settings, repository, "overmind_not_connected")
        try:
            result = _sync_rom_metadata_to_overmind_locked(settings, repository, config, base_url, token, prepared_poll=prepared_poll)
        except Exception:
            _defer_rom_metadata_upload(settings, "overmind_upload_failed")
            raise
        # Best-effort: a saves sync failure must not affect ROM metadata results.
        try:
            _sync_saves_to_overmind(settings, base_url, token)
        except Exception as error:
            _overmind_log(f"Saves sync failed: error={_format_overmind_error(error)}")
        return result
    finally:
        _end_rom_metadata_activity()
