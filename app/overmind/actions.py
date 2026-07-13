"""Overmind action dispatcher.

Extracted from ``drone_api.py``. ``_execute_overmind_action`` maps an Overmind
``action`` dict (collect/rebuild asset metadata, screen/volume/idle-automation control,
ES restart, self-update, config pushes, ...) to its side effect and returns
``(status, message, result)``.
"""

import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional, Tuple

try:
    from ..common.settings import Settings
    from ..device.automation import (
        _load_automation_config,
        _reset_idle_game_exit_armed_state,
        _reset_idle_volume_armed_state,
        _reset_wifi_recovery_check_state,
        _save_automation_config,
    )
    from ..device.device_control import _apply_audio_volume, _apply_screen_mode, _restart_emulationstation
    from ..device.es_collections import apply_es_collections as _apply_es_collections
    from ..device.es_collections import get_es_collections_state as _get_es_collections_state
    from ..device.pixen import run_pixen_upgrade
    from ..storage.rom_metadata_store import (
        _clear_sqlite_asset_metadata_cache,
        _empty_rom_metadata_cache,
        _persist_rom_metadata_cache,
        _purge_asset_cache_keep_fingerprint,
    )
    from ..transfer.peer_download import (
        _best_peer_for_bios,
        _best_peer_for_rom,
        _cached_rom_fingerprint_exists,
        _post_rom_sync_activity,
        _resolve_rom_by_gamelist_id_from_peer,
    )
    from ..transfer.transfer_files import bios_md5_exists as _bios_md5_exists
    from .overmind_config import _load_overmind_config_for_settings
    from .overmind_game_logs import load_gameplay_history as _load_gameplay_history
    from .registration import _summarize_overmind_result
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from device.automation import (  # type: ignore
        _load_automation_config,
        _reset_idle_game_exit_armed_state,
        _reset_idle_volume_armed_state,
        _reset_wifi_recovery_check_state,
        _save_automation_config,
    )
    from device.device_control import _apply_audio_volume, _apply_screen_mode, _restart_emulationstation  # type: ignore
    from device.es_collections import apply_es_collections as _apply_es_collections  # type: ignore
    from device.es_collections import get_es_collections_state as _get_es_collections_state  # type: ignore
    from device.pixen import run_pixen_upgrade  # type: ignore
    from storage.rom_metadata_store import (  # type: ignore
        _clear_sqlite_asset_metadata_cache,
        _empty_rom_metadata_cache,
        _persist_rom_metadata_cache,
        _purge_asset_cache_keep_fingerprint,
    )
    from transfer.peer_download import (  # type: ignore
        _best_peer_for_bios,
        _best_peer_for_rom,
        _cached_rom_fingerprint_exists,
        _post_rom_sync_activity,
        _resolve_rom_by_gamelist_id_from_peer,
    )
    from transfer.transfer_files import bios_md5_exists as _bios_md5_exists  # type: ignore
    from overmind.overmind_config import _load_overmind_config_for_settings  # type: ignore
    from overmind.overmind_game_logs import load_gameplay_history as _load_gameplay_history  # type: ignore
    from overmind.registration import _summarize_overmind_result  # type: ignore


# Friendly-name counterpart of set_es_collections.RESTART_REQUIRED_FIELDS (that
# module uses the low-level field names written to es_settings.cfg; this is the
# set_es_collections action's friendly payload keys) -- screensaver_minutes is
# the only one that doesn't restart EmulationStation.
_ES_RESTART_REQUIRED_UPDATE_KEYS = {"music_volume", "hidden_systems", "ungrouped_systems", "auto_collections", "custom_collections"}


def _execute_overmind_action(
    settings: Settings,
    repository: "RomRepository",
    action: dict,
    config: Optional[dict] = None,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
) -> Tuple[str, str, Optional[dict]]:
    # _ROM_METADATA_WAKE + _collect_rom_metadata + _get_download_manager stay in drone_api; lazy-import to avoid a cycle.
    try:
        from ..drone_api import (
            ARTWORK_FIELDS,
            DRONE_REMOTE_REBOOT_EXIT_CODE,
            _ROM_METADATA_WAKE,
            _collect_rom_metadata,
            _get_download_manager,
        )
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import (  # type: ignore
            ARTWORK_FIELDS,
            DRONE_REMOTE_REBOOT_EXIT_CODE,
            _ROM_METADATA_WAKE,
            _collect_rom_metadata,
            _get_download_manager,
        )
    action_name = str(action.get("action") or "").strip().lower()

    if action_name == "collect_rom_metadata":
        result = _collect_rom_metadata(settings, repository)
        return "completed", f"Collected {_summarize_overmind_result(result)}.", result

    if action_name == "rebuild_asset_metadata":
        _clear_sqlite_asset_metadata_cache(settings)
        cache = _empty_rom_metadata_cache()
        cache["dirty"] = True
        cache["full_refresh_pending"] = True
        cache["rebuild_requested_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        _persist_rom_metadata_cache(settings, cache)
        _ROM_METADATA_WAKE.set()
        return "completed", "Queued full asset metadata rebuild; local asset cache was cleared and the metadata poller will upload a fresh snapshot.", {
            "type": "asset_metadata_rebuild",
            "status": "queued",
            "reason": "local_asset_cache_cleared",
            "poller_wake_requested": True,
        }

    if action_name == "purge_asset_cache":
        result = _purge_asset_cache_keep_fingerprint(settings)
        _ROM_METADATA_WAKE.set()
        return "completed", "Queued asset cache purge; cached fingerprint values were kept, and the metadata poller will re-scan and upload a fresh full inventory.", {
            "type": "asset_cache_purge",
            "status": result.get("status", "queued"),
            "reason": "full_refresh_kept_fingerprint",
            "poller_wake_requested": True,
        }

    if action_name == "collect_game_logs":
        sessions = _load_gameplay_history(settings)
        result = {"type": "game_logs", "sessions": sessions}
        return "completed", f"Collected {_summarize_overmind_result(result)}.", result

    if action_name in ("collect_emulator_configs", "collect_log_sources"):
        # Emulator configs and drone/ES logs are no longer collected or uploaded to
        # Overmind. Answer any stale request from an older Overmind as a no-op.
        return "skipped", "Emulator configs and logs are no longer reported to Overmind.", {"type": action_name, "removed": True}

    if action_name == "sync_bios":
        config = _load_overmind_config_for_settings(settings)
        manager = _get_download_manager()
        if manager is None:
            return "failed", "Download manager is not available.", None
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        rel = str(payload.get("file_path") or payload.get("relative_path") or payload.get("bios_name") or payload.get("name") or "").strip()
        expected_md5 = payload.get("bios_md5") or payload.get("fingerprint")
        source_device_ids = {
            str(device.get("device_id") or device.get("drone_id") or "")
            for device in payload.get("devices", [])
            if isinstance(device, dict)
        }
        if not rel:
            return "failed", "BIOS path is required.", None
        sync_id = str(uuid.uuid4())
        started_wall = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        started_mono = time.monotonic()
        if expected_md5 and _bios_md5_exists(repository, expected_md5):
            result = {
                "type": "bios_sync",
                "activity": [{
                    "asset_type": "bios",
                    "sync_id": sync_id,
                    "target_drone_id": settings.overmind_device_id,
                    "system": "bios",
                    "bios_name": rel,
                    "relative_path": rel,
                    "action": "download",
                    "status": "skipped",
                    "failure_reason": "BIOS fingerprint already exists locally",
                    "bios_md5": expected_md5,
                    "download_started_at": started_wall,
                    "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "duration_ms": int((time.monotonic() - started_mono) * 1000),
                }],
            }
            return "completed", "BIOS sync skipped because matching file already exists.", result
        peer = _best_peer_for_bios(settings, config, rel, source_device_ids=source_device_ids)
        if not peer:
            if source_device_ids:
                # Overmind named at least one Drone that has this BIOS; none is
                # reachable right now. Hold it 'pending' instead of failing outright
                # -- the download manager's worker retries resolution until one
                # answers (see DownloadManager._retry_pending_job).
                activity = manager.enqueue_pending_bios(
                    config,
                    rel,
                    expected_size=payload.get("file_size"),
                    expected_md5=expected_md5,
                    source_action_id=str(action.get("id") or ""),
                    sync_id=sync_id,
                    source_device_ids=source_device_ids,
                )
                activity["sync_id"] = sync_id
                return "completed", "BIOS sync is pending a reachable source Drone.", {"type": "bios_sync", "activity": [activity]}
            result = {
                "type": "bios_sync",
                "activity": [{
                    "asset_type": "bios",
                    "sync_id": sync_id,
                    "target_drone_id": settings.overmind_device_id,
                    "system": "bios",
                    "bios_name": rel,
                    "relative_path": rel,
                    "action": "download",
                    "status": "failed",
                    "failure_reason": "No healthy source peer with requested BIOS found",
                    "bios_md5": expected_md5,
                    "download_started_at": started_wall,
                    "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "duration_ms": int((time.monotonic() - started_mono) * 1000),
                }],
            }
            return "failed", "BIOS sync failed: no healthy source peer.", result
        try:
            activity = manager.enqueue_bios(
                config,
                peer,
                rel,
                expected_size=payload.get("file_size"),
                expected_md5=expected_md5,
                source_action_id=str(action.get("id") or ""),
            )
            activity["sync_id"] = activity.get("job_id") or sync_id
            activity["bios_md5"] = activity.get("bios_md5") or expected_md5
            return "completed", "BIOS sync queued 1 item.", {"type": "bios_sync", "activity": [activity]}
        except Exception as error:
            result = {
                "type": "bios_sync",
                "activity": [{
                    "asset_type": "bios",
                    "sync_id": sync_id,
                    "source_drone_id": str(peer.get("drone_id") or peer.get("device_id") or ""),
                    "target_drone_id": settings.overmind_device_id,
                    "system": "bios",
                    "bios_name": rel,
                    "relative_path": rel,
                    "action": "download",
                    "status": "failed",
                    "failure_reason": str(error),
                    "bios_md5": expected_md5,
                    "download_started_at": started_wall,
                    "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "duration_ms": int((time.monotonic() - started_mono) * 1000),
                }],
            }
            return "failed", "BIOS sync failed for 1 item.", result

    if action_name == "sync_artwork":
        config = _load_overmind_config_for_settings(settings)
        manager = _get_download_manager()
        if manager is None:
            return "failed", "Download manager is not available.", None
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        system = str(payload.get("system_name") or payload.get("system") or "").strip()
        rom_path = str(payload.get("rom_path") or payload.get("file_path") or payload.get("rom_name") or "").strip()
        artwork_type = str(payload.get("artwork_type") or "").strip()
        source_device_ids = {
            str(device.get("device_id") or device.get("drone_id") or "")
            for device in payload.get("devices", [])
            if isinstance(device, dict)
        }
        if not system or not rom_path or artwork_type not in ARTWORK_FIELDS:
            return "failed", "system, rom_path, and artwork_type are required.", None
        sync_id = str(uuid.uuid4())
        started_wall = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        peer = _best_peer_for_bios(settings, config, rom_path, source_device_ids=source_device_ids)
        if not peer:
            result = {"type": "artwork_sync", "activity": [{
                "asset_type": "artwork",
                "sync_id": sync_id,
                "target_drone_id": settings.overmind_device_id,
                "system": system,
                "rom_name": rom_path,
                "rom_path": rom_path,
                "artwork_type": artwork_type,
                "relative_path": rom_path,
                "action": "download",
                "status": "failed",
                "failure_reason": "No healthy source peer with requested artwork found",
                "download_started_at": started_wall,
                "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }]}
            return "failed", "Artwork sync failed: no healthy source peer.", result
        activity = manager.enqueue_artwork(
            config,
            peer,
            system,
            rom_path,
            artwork_type,
            source_action_id=str(action.get("id") or ""),
        )
        activity["sync_id"] = activity.get("job_id") or sync_id
        return "completed", "Artwork sync queued 1 item.", {"type": "artwork_sync", "activity": [activity]}

    if action_name in {"sync_rom", "sync_system"}:
        config = _load_overmind_config_for_settings(settings)
        manager = _get_download_manager()
        if manager is None:
            return "failed", "Download manager is not available.", None
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        requested = []
        if action_name == "sync_rom":
            requested = [payload]
        else:
            requested = payload.get("roms") if isinstance(payload.get("roms"), list) else []
        activities = []
        failures = 0
        for item in requested:
            system = str(item.get("system_name") or item.get("system") or payload.get("system_name") or "").strip()
            gamelist_id = str(item.get("gamelist_id") or "").strip()
            # Overmind identifies the ROM by its gamelist <game id>; the sender path is
            # resolved from the source peer's own gamelist below. file_path/relative_path
            # may still arrive from a legacy Overmind, in which case we use it directly.
            rel = str(item.get("file_path") or item.get("relative_path") or "").strip()
            expected_fingerprint = item.get("rom_fingerprint") or item.get("fingerprint")
            entry_type = str(item.get("entry_type") or "file").strip().lower()
            sync_id = str(item.get("sync_id") or payload.get("sync_id") or uuid.uuid4())
            source_device_ids = {
                str(device.get("device_id") or device.get("drone_id") or "")
                for device in item.get("devices", [])
                if isinstance(device, dict)
            }
            if not system or (not rel and not gamelist_id):
                continue
            display_name = rel or gamelist_id
            resolved_artwork_types = []
            marker_relative_path = None
            started_wall = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            started_mono = time.monotonic()
            if expected_fingerprint and _cached_rom_fingerprint_exists(settings, expected_fingerprint):
                activity = {
                    "sync_id": sync_id,
                    "target_drone_id": settings.overmind_device_id,
                    "system": system,
                    "rom_name": display_name,
                    "relative_path": rel or display_name,
                    "entry_type": entry_type,
                    "action": "download",
                    "status": "skipped",
                    "failure_reason": "ROM fingerprint already exists locally",
                    "rom_fingerprint": expected_fingerprint,
                    "download_started_at": started_wall,
                    "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "duration_ms": int((time.monotonic() - started_mono) * 1000),
                }
                _post_rom_sync_activity(settings, config, activity)
                activities.append(activity)
                continue
            peer = _best_peer_for_rom(settings, repository, config, system, rel, source_device_ids=source_device_ids)
            if not peer:
                if source_device_ids:
                    # Overmind named at least one Drone that has this ROM; none is
                    # reachable right now. Hold it 'pending' instead of failing --
                    # the download manager's worker retries resolution (and, if only
                    # a gamelist_id was given, path resolution) until one answers.
                    activity = manager.enqueue_pending_rom(
                        config,
                        system,
                        gamelist_id=gamelist_id,
                        relative_path=rel,
                        expected_size=item.get("file_size"),
                        expected_fingerprint=expected_fingerprint,
                        source_action_id=str(action.get("id") or ""),
                        entry_type=entry_type,
                        sync_id=sync_id,
                        source_device_ids=source_device_ids,
                    )
                    activity["sync_id"] = sync_id
                    activity["entry_type"] = activity.get("entry_type") or entry_type
                    activities.append(activity)
                    continue
                failures += 1
                activity = {
                    "sync_id": sync_id,
                    "target_drone_id": settings.overmind_device_id,
                    "system": system,
                    "rom_name": display_name,
                    "relative_path": rel or display_name,
                    "entry_type": entry_type,
                    "action": "download",
                    "status": "failed",
                    "failure_reason": "No healthy source peer with requested ROM found",
                    "rom_fingerprint": expected_fingerprint,
                    "download_started_at": started_wall,
                    "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "duration_ms": int((time.monotonic() - started_mono) * 1000),
                }
                _post_rom_sync_activity(settings, config, activity)
                activities.append(activity)
                continue
            if not rel and gamelist_id:
                # Map the gamelist id -> the sender's own ROM path before pulling bytes.
                resolved = _resolve_rom_by_gamelist_id_from_peer(settings, config, peer, system, gamelist_id)
                if not resolved or not resolved.get("relative_path"):
                    failures += 1
                    activity = {
                        "sync_id": sync_id,
                        "source_drone_id": str(peer.get("drone_id") or peer.get("device_id") or ""),
                        "target_drone_id": settings.overmind_device_id,
                        "system": system,
                        "rom_name": display_name,
                        "relative_path": display_name,
                        "entry_type": entry_type,
                        "action": "download",
                        "status": "failed",
                        "failure_reason": "Source peer could not resolve ROM by gamelist id",
                        "rom_fingerprint": expected_fingerprint,
                        "download_started_at": started_wall,
                        "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                        "duration_ms": int((time.monotonic() - started_mono) * 1000),
                    }
                    _post_rom_sync_activity(settings, config, activity)
                    activities.append(activity)
                    continue
                rel = str(resolved.get("relative_path") or "").strip()
                entry_type = str(resolved.get("entry_type") or entry_type).strip().lower()
                # Folder-unit ROMs: rel is the per-game folder; the marker (the
                # sender's gamelist <path>) carries identity + artwork lookup.
                marker_relative_path = str(resolved.get("marker_relative_path") or "").strip() or None
                if not expected_fingerprint:
                    expected_fingerprint = resolved.get("rom_fingerprint")
                if not item.get("file_size") and resolved.get("file_size"):
                    item = {**item, "file_size": resolved.get("file_size")}
                resolved_artwork_types = resolved.get("artwork_types") if isinstance(resolved.get("artwork_types"), list) else []
            try:
                activity = manager.enqueue_rom(
                    config,
                    peer,
                    system,
                    rel,
                    expected_size=item.get("file_size"),
                    expected_fingerprint=expected_fingerprint,
                    source_action_id=str(action.get("id") or ""),
                    entry_type=entry_type,
                    sync_id=sync_id,
                    artwork_types=resolved_artwork_types,
                    marker_relative_path=marker_relative_path,
                )
                activity["sync_id"] = sync_id
                activity["rom_fingerprint"] = activity.get("rom_fingerprint") or expected_fingerprint
                activity["entry_type"] = activity.get("entry_type") or entry_type
                activities.append(activity)
            except Exception as error:
                failures += 1
                activity = {
                    "sync_id": sync_id,
                    "source_drone_id": str(peer.get("drone_id") or peer.get("device_id") or ""),
                    "target_drone_id": settings.overmind_device_id,
                    "system": system,
                    "rom_name": rel,
                    "relative_path": rel,
                    "entry_type": entry_type,
                    "action": "download",
                    "status": "failed",
                    "failure_reason": str(error),
                    "rom_fingerprint": expected_fingerprint,
                    "download_started_at": started_wall,
                    "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "duration_ms": int((time.monotonic() - started_mono) * 1000),
                }
                _post_rom_sync_activity(settings, config, activity)
                activities.append(activity)
        result = {"type": "rom_sync", "activity": activities}
        if failures and failures == len(activities):
            return "failed", f"ROM sync failed for {failures} item(s).", result
        return "completed", f"ROM sync queued {len(activities)} item(s) with {failures} failure(s).", result

    if action_name == "cancel_download":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        job_id = str(payload.get("job_id") or payload.get("download_id") or "").strip()
        if not job_id:
            return "failed", "job_id is required.", None
        manager = _get_download_manager()
        if manager is None:
            return "failed", "Download manager is not available.", None
        result = manager.cancel(job_id, "cancelled from Overmind")
        status_value = "completed" if result.get("status") != "not_found" else "failed"
        return status_value, f"Cancel request for download {job_id}: {result.get('status')}.", {"type": "download_cancel", **result}

    if action_name == "pause_download":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        job_id = str(payload.get("job_id") or payload.get("download_id") or "").strip()
        if not job_id:
            return "failed", "job_id is required.", None
        manager = _get_download_manager()
        if manager is None:
            return "failed", "Download manager is not available.", None
        result = manager.pause_job(job_id)
        status_value = "completed" if result.get("status") not in {"not_found", "not_pausable"} else "failed"
        return status_value, f"Pause request for download {job_id}: {result.get('status')}.", {"type": "download_pause", **result}

    if action_name == "resume_download":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        job_id = str(payload.get("job_id") or payload.get("download_id") or "").strip()
        if not job_id:
            return "failed", "job_id is required.", None
        manager = _get_download_manager()
        if manager is None:
            return "failed", "Download manager is not available.", None
        result = manager.resume_job(job_id)
        status_value = "completed" if result.get("status") not in {"not_found", "not_resumable"} else "failed"
        return status_value, f"Resume request for download {job_id}: {result.get('status')}.", {"type": "download_resume", **result}

    if action_name == "shutdown":
        return "failed", "Unsupported action: shutdown is disabled by Overmind safety policy.", None

    if action_name == "restart":
        if settings.use_fake_data:
            return "completed", "Simulated restart action because USE_FAKE_DATA is enabled.", None
        return "completed", "Host reboot requested; Drone service supervisor will reboot after action completion is reported.", {
            "type": "system_restart",
            "reboot_requested": True,
            "exit_code": DRONE_REMOTE_REBOOT_EXIT_CODE,
        }

    if action_name == "refresh_emulator_list":
        if settings.use_fake_data:
            return "completed", "Simulated emulator list refresh because USE_FAKE_DATA is enabled.", {
                "type": "emulator_list_refresh",
                "emulationstation_restarted": False,
                "simulated": True,
            }
        if not _restart_emulationstation():
            return "failed", "Unable to refresh emulator list: EmulationStation restart command was not found.", {
                "type": "emulator_list_refresh",
                "emulationstation_restarted": False,
            }
        return "completed", "Emulator list refresh issued through an EmulationStation restart.", {
            "type": "emulator_list_refresh",
            "emulationstation_restarted": True,
        }

    if action_name == "run_pixen_update":
        try:
            result = run_pixen_upgrade(settings)
        except FileNotFoundError:
            return "failed", "PixeN update script was not found on this Drone.", {
                "type": "pixen_update",
                "status": "missing",
            }
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            return "failed", f"Unable to start PixeN update: {error}", {
                "type": "pixen_update",
                "status": "failed",
                "error": str(error),
            }
        return "completed", "PixeN update script started.", result

    if action_name == "set_screen_mode":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        mode = str(payload.get("mode") or "").strip().lower()
        if mode not in {"full", "kiosk", "kid"}:
            return "failed", "A screen mode of full, kiosk, or kid is required.", None
        try:
            settings_path, restarted = _apply_screen_mode(settings, mode)
        except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError) as error:
            return "failed", f"Unable to update screen mode settings: {error}", None
        suffix = " EmulationStation restart issued." if restarted else " Applies on the next EmulationStation restart."
        return "completed", f"Screen mode set to {mode}.{suffix}", {
            "type": "screen_mode",
            "mode": mode,
            "settings_file": str(settings_path),
            "emulationstation_restarted": restarted,
        }

    if action_name == "set_volume":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        raw_level = payload.get("level")
        if raw_level is None:
            raw_level = payload.get("volume")
        try:
            level = int(raw_level)
        except (TypeError, ValueError):
            return "failed", "A numeric volume level (0-100) is required.", None
        try:
            applied = _apply_audio_volume(settings, level)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            return "failed", f"Unable to set volume: {error}", None
        label = "muted" if applied <= 0 else f"set to {applied}%"
        return "completed", f"Volume {label}.", {
            "type": "audio_volume",
            "level": applied,
            "muted": applied <= 0,
        }

    if action_name == "get_es_collections_state":
        try:
            state = _get_es_collections_state(settings)
        except Exception as error:
            return "failed", f"Unable to read EmulationStation collections state: {error}", None
        return "completed", "EmulationStation collections state collected.", {
            "type": "es_collections_state",
            **state,
        }

    if action_name == "set_music_volume":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        raw_level = payload.get("level")
        try:
            level = int(raw_level)
        except (TypeError, ValueError):
            return "failed", "A numeric music volume level (0-100) is required.", None
        try:
            state = _apply_es_collections(settings, {"music_volume": max(0, min(100, level))})
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            return "failed", f"Unable to set music volume: {error}", None
        return "completed", f"Music volume set to {state['music_volume']}%; EmulationStation restarted.", {
            "type": "es_collections_state",
            **state,
        }

    if action_name == "set_es_collections":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        updates = {
            key: payload[key]
            for key in ("music_volume", "screensaver_minutes", "hidden_systems", "ungrouped_systems", "auto_collections", "custom_collections")
            if key in payload
        }
        if not updates:
            return "failed", "No recognized collections fields were provided.", None
        try:
            state = _apply_es_collections(settings, updates)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            return "failed", f"Unable to update EmulationStation collections: {error}", None
        # music_volume/screensaver_minutes apply live; everything else here changes
        # which systems/collections are shown and restarts EmulationStation.
        restarted = bool(_ES_RESTART_REQUIRED_UPDATE_KEYS.intersection(updates))
        suffix = " EmulationStation restarted." if restarted else ""
        return "completed", f"EmulationStation collections updated ({', '.join(sorted(updates))}).{suffix}", {
            "type": "es_collections_state",
            **state,
        }

    if action_name == "set_idle_volume_automation":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        config = _load_automation_config(settings)
        merged = {**config["idle_volume"], **payload}
        saved = _save_automation_config(settings, {"idle_volume": merged})["idle_volume"]
        # Re-evaluate from scratch against the new settings on the next poll tick.
        _reset_idle_volume_armed_state()
        state = "enabled" if saved["enabled"] else "disabled"
        message = (
            f"Idle volume automation {state}: set to {saved['target_volume']}% "
            f"after {saved['idle_minutes']} min of no input."
        )
        return "completed", message, {"type": "idle_volume_automation", **saved}

    if action_name == "set_idle_game_exit_automation":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        config = _load_automation_config(settings)
        merged = {**config["idle_game_exit"], **payload}
        saved = _save_automation_config(settings, {"idle_game_exit": merged})["idle_game_exit"]
        # Re-evaluate from scratch against the new settings on the next poll tick.
        _reset_idle_game_exit_armed_state()
        state = "enabled" if saved["enabled"] else "disabled"
        message = (
            f"Idle game-exit automation {state}: exit the running game "
            f"after {saved['idle_minutes']} min of no input."
        )
        return "completed", message, {"type": "idle_game_exit_automation", **saved}

    if action_name == "set_wifi_recovery_automation":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        config = _load_automation_config(settings)
        merged = {**config["wifi_recovery"], **payload}
        saved = _save_automation_config(settings, {"wifi_recovery": merged})["wifi_recovery"]
        _reset_wifi_recovery_check_state()
        state = "enabled" if saved["enabled"] else "disabled"
        message = f"Wi-Fi recovery automation {state}: check the wireless connection every 60 seconds."
        return "completed", message, {"type": "wifi_recovery_automation", **saved}

    if action_name == "update":
        if settings.use_fake_data:
            return "completed", "Simulated update action because USE_FAKE_DATA is enabled.", None
        updater = shutil.which("batocera-upgrade")
        if not updater:
            return "failed", "batocera-upgrade command was not found", None
        subprocess.Popen([updater], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "completed", "Batocera update command issued.", None

    return "failed", f"Unsupported action: {action_name}", None
