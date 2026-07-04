"""Overmind heartbeat + action-poll loop.

Extracted from ``drone_api.py``. ``_start_overmind_action_poller`` runs the background loop
that (on cadence) sends the device heartbeat with system info/metrics/thumbprints, reports
emulator-config + log-source + game-event data, polls for Overmind actions and executes
them, and handles token reclaim. The running singleton (``_get_download_manager``) stays in
``drone_api``; cadence knobs are local copies (same env).
"""

import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from threading import Thread
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote

try:
    from ..common.runtime_state import _ROM_METADATA_ACTIVE
    from ..transfer import local_network as _local_network
    from ..common.settings import Settings
    from ..common.logging_setup import (
        _overmind_log,
    )
    from ..device.automation import (
        _load_automation_config,
    )
    from ..device.device_control import (
        _get_audio_volume,
        _get_screen_mode,
    )
    from ..device.system_info import (
        _collect_system_info_payload,
    )
    from ..device.system_metrics import (
        _collect_performance_metrics,
        _sample_speed,
    )
    from ..overmind.actions import (
        _execute_overmind_action,
    )
    from ..overmind.collectors import (
        OVERMIND_EVENT_TYPES,
        _filesystem_events,
    )
    from ..overmind.heartbeat_sync import (
        _local_asset_thumbprints,
        _maybe_request_asset_push_from_heartbeat,
    )
    from ..overmind.overmind_client import (
        _format_overmind_error,
        _overmind_post_json,
        _overmind_post_json_with_status,
    )
    from ..overmind.overmind_config import (
        _load_overmind_config_for_settings,
        _overmind_swarm_path_for_settings,
    )
    from ..overmind.overmind_filesystem import (
        filesystem_snapshot as _filesystem_snapshot,
    )
    from ..overmind.overmind_game_logs import (
        collect_game_event_sessions as _collect_game_event_sessions,
        delete_game_event_spool as _delete_game_event_spool,
    )
    from ..overmind.registration import (
        _reclaim_overmind_token_after_unauthorized,
        _record_processed_overmind_action,
        _register_or_claim_overmind_token,
        _report_overmind_action_completion,
    )
    from ..roms.rom_inventory import (
        ROM_INVENTORY_FINGERPRINT_ALGORITHM,
        _rom_inventory_fingerprint_from_cache_state,
    )
    from ..storage.state_store import (
        database_path as _state_database_path,
        save_payload as _save_state_payload,
    )
    from ..transfer.drone_network import (
        _drone_advertised_api_port,
        _drone_network_payload,
        _drone_reachable_url,
        _drone_report_host,
        _get_local_ip_addresses,
    )
    from ..transfer.drone_tls import (
        DroneCertificateManager,
    )
    from ..transfer.network_identity import (
        drone_scheme as _drone_scheme,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.runtime_state import _ROM_METADATA_ACTIVE  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from common.settings import Settings  # type: ignore
    from common.logging_setup import (
        _overmind_log,
    )
    from device.automation import (
        _load_automation_config,
    )
    from device.device_control import (
        _get_audio_volume,
        _get_screen_mode,
    )
    from device.system_info import (
        _collect_system_info_payload,
    )
    from device.system_metrics import (
        _collect_performance_metrics,
        _sample_speed,
    )
    from overmind.actions import (
        _execute_overmind_action,
    )
    from overmind.collectors import (
        OVERMIND_EVENT_TYPES,
        _filesystem_events,
    )
    from overmind.heartbeat_sync import (
        _local_asset_thumbprints,
        _maybe_request_asset_push_from_heartbeat,
    )
    from overmind.overmind_client import (
        _format_overmind_error,
        _overmind_post_json,
        _overmind_post_json_with_status,
    )
    from overmind.overmind_config import (
        _load_overmind_config_for_settings,
        _overmind_swarm_path_for_settings,
    )
    from overmind.overmind_filesystem import (
        filesystem_snapshot as _filesystem_snapshot,
    )
    from overmind.overmind_game_logs import (
        collect_game_event_sessions as _collect_game_event_sessions,
        delete_game_event_spool as _delete_game_event_spool,
    )
    from overmind.registration import (
        _reclaim_overmind_token_after_unauthorized,
        _record_processed_overmind_action,
        _register_or_claim_overmind_token,
        _report_overmind_action_completion,
    )
    from roms.rom_inventory import (
        ROM_INVENTORY_FINGERPRINT_ALGORITHM,
        _rom_inventory_fingerprint_from_cache_state,
    )
    from storage.state_store import (
        database_path as _state_database_path,
        save_payload as _save_state_payload,
    )
    from transfer.drone_network import (
        _drone_advertised_api_port,
        _drone_network_payload,
        _drone_reachable_url,
        _drone_report_host,
        _get_local_ip_addresses,
    )
    from transfer.drone_tls import (
        DroneCertificateManager,
    )
    from transfer.network_identity import (
        drone_scheme as _drone_scheme,
    )

OVERMIND_SPEED_SAMPLE_SECONDS = int(os.environ.get("OVERMIND_SPEED_SAMPLE_SECONDS", "600"))
OVERMIND_HEARTBEAT_SECONDS = int(os.environ.get("OVERMIND_POLL_SECONDS", "30"))
OVERMIND_HEARTBEAT_TIMEOUT_SECONDS = max(10, int(os.environ.get("OVERMIND_HEARTBEAT_TIMEOUT_SECONDS", "20")))
DRONE_REMOTE_REBOOT_EXIT_CODE = 76


def _get_download_manager():
    """Delegate to the drone_api singleton accessor (lazy to avoid a cycle)."""
    try:
        from ..drone_api import _get_download_manager as _impl
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import _get_download_manager as _impl  # type: ignore
    return _impl()
def _start_overmind_action_poller(settings: Settings, repository: "RomRepository") -> None:
    poll_seconds = max(5, int(settings.overmind_poll_seconds or OVERMIND_HEARTBEAT_SECONDS))
    speed_sample_seconds = OVERMIND_SPEED_SAMPLE_SECONDS
    system_info_refresh_seconds = max(300, int(os.environ.get("DRONE_SYSTEM_INFO_REFRESH_SECONDS", "3600")))
    last_speed_sample_at: Optional[float] = None
    last_system_info_at = -float(system_info_refresh_seconds)
    system_info_payload: dict = {}
    fs_snapshot = _filesystem_snapshot(settings)

    def loop() -> None:
        nonlocal last_speed_sample_at, last_system_info_at, system_info_payload, fs_snapshot
        while True:
            if not _local_network.is_overmind_mode(settings):
                time.sleep(poll_seconds)
                continue
            try:
                config = _load_overmind_config_for_settings(settings)
                base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
                token = str(config.get("overmind_token") or "").strip()
                integration_enabled = bool(config.get("integration_enabled"))
                if not base_url:
                    time.sleep(poll_seconds)
                    continue
                if not token:
                    auth_token = str(config.get("overmind_auth_token") or "").strip()
                    if auth_token and integration_enabled:
                        token = _register_or_claim_overmind_token(settings, repository, config, base_url) or ""
                        if not token:
                            # Pending onboarding still emits a lightweight heartbeat. Overmind
                            # records it as an installed, recoverable Drone until approval.
                            token = auth_token
                    if not token:
                        time.sleep(poll_seconds)
                        continue

                device_id = quote(settings.overmind_device_id, safe="")
                now = time.monotonic()
                if not system_info_payload or now - last_system_info_at >= system_info_refresh_seconds:
                    system_info_payload = _collect_system_info_payload(settings)
                    last_system_info_at = now
                else:
                    system_info_payload["performance"] = _collect_performance_metrics(settings.userdata_root)
                    system_info_payload["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                    system_info_payload["screen_mode"] = _get_screen_mode(settings)
                    system_info_payload["audio_volume"] = _get_audio_volume(settings)
                    system_info_payload["idle_volume_automation"] = _load_automation_config(settings)["idle_volume"]
                network_payload = _drone_network_payload(settings)
                heartbeat_payload = {
                    "device_id": settings.overmind_device_id,
                    "device_name": str(config.get("drone_name") or "").strip() or socket.gethostname(),
                    "network": network_payload,
                    "api_port": _drone_advertised_api_port(settings),
                    "scheme": _drone_scheme(settings),
                    "reachable_url": _drone_reachable_url(settings, network_payload),
                    "certificate": DroneCertificateManager(settings).metadata(),
                    "system_info": system_info_payload,
                    "downloads": _get_download_manager().snapshot() if _get_download_manager() else {},
                }
                rom_fingerprint = _rom_inventory_fingerprint_from_cache_state(settings)
                if rom_fingerprint:
                    heartbeat_payload["rom_inventory_fingerprint"] = rom_fingerprint
                    heartbeat_payload["rom_inventory_fingerprint_algorithm"] = ROM_INVENTORY_FINGERPRINT_ALGORITHM
                local_romset_thumbprint, local_bios_thumbprint = _local_asset_thumbprints(settings)
                if local_romset_thumbprint:
                    heartbeat_payload["romset_files_thumbprint"] = local_romset_thumbprint
                if local_bios_thumbprint:
                    heartbeat_payload["bios_files_thumbprint"] = local_bios_thumbprint
                heartbeat_url = f"{base_url}/api/devices/{device_id}/heartbeat"
                heartbeat_started = time.monotonic()
                _overmind_log(
                    f"Heartbeat send started: endpoint={heartbeat_url} device_id={settings.overmind_device_id}"
                )
                try:
                    try:
                        status_code, response = _overmind_post_json_with_status(
                            heartbeat_url,
                            heartbeat_payload,
                            token=token,
                            settings=settings,
                            timeout_seconds=OVERMIND_HEARTBEAT_TIMEOUT_SECONDS,
                        )
                    except HTTPError as error:
                        if error.code == 401 and integration_enabled:
                            replacement_token = _reclaim_overmind_token_after_unauthorized(settings, repository, config, base_url, error)
                            if replacement_token:
                                token = replacement_token
                                status_code, response = _overmind_post_json_with_status(
                                    heartbeat_url,
                                    heartbeat_payload,
                                    token=token,
                                    settings=settings,
                                    timeout_seconds=OVERMIND_HEARTBEAT_TIMEOUT_SECONDS,
                                )
                            else:
                                raise
                        else:
                            raise
                except Exception as error:
                    status_part = f" status={error.code}" if isinstance(error, HTTPError) else ""
                    _overmind_log(
                        f"Heartbeat send failed: endpoint={heartbeat_url}{status_part} error={_format_overmind_error(error)} duration_ms={int((time.monotonic() - heartbeat_started) * 1000)}"
                    )
                    raise
                _overmind_log(
                    f"Heartbeat send succeeded: endpoint={heartbeat_url} status={status_code} duration_ms={int((time.monotonic() - heartbeat_started) * 1000)}"
                )
                if not integration_enabled:
                    time.sleep(poll_seconds)
                    continue
                swarm = response.get("swarm") if isinstance(response.get("swarm"), list) else []
                _save_state_payload(_state_database_path(settings.userdata_root), "overmind_swarm.json", swarm)
                _overmind_swarm_path_for_settings(settings).unlink(missing_ok=True)

                # Overmind echoes the asset thumbprints it last stored for this Drone.
                # If they differ from what the Drone currently holds, Overmind's copy has
                # drifted (or is missing) — wake the metadata poller to push a fresh
                # inventory and resync. The Drone, not Overmind, decides to resend.
                _maybe_request_asset_push_from_heartbeat(settings, response)

                # Telemetry steps below are best-effort and independent: a failure in
                # one (e.g. a flaky speed test) must not abort the heartbeat iteration
                # before later steps such as the game-log upload get a chance to run.
                if speed_sample_seconds > 0 and (
                    last_speed_sample_at is None or now - last_speed_sample_at >= speed_sample_seconds
                ):
                    speed_url = f"{base_url}/api/devices/{device_id}/speed"
                    try:
                        speed_sample = _sample_speed()
                        _overmind_post_json(speed_url, speed_sample, token=token, settings=settings)
                        _overmind_post_json(
                            f"{base_url}/api/devices/{device_id}/events",
                            {
                                "drone_id": settings.overmind_device_id,
                                "event_type": OVERMIND_EVENT_TYPES["speed"],
                                "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                                "metadata": {"speed_result": speed_sample},
                            },
                            token=token,
                            settings=settings,
                        )
                        _overmind_log(f"Speed sample sent to Overmind for {settings.overmind_device_id}")
                        last_speed_sample_at = now
                    except Exception as error:
                        _overmind_log(f"Speed sample failed for {settings.overmind_device_id}; continuing: {_format_overmind_error(error)}")

                if not _ROM_METADATA_ACTIVE.is_set():
                    try:
                        next_fs_snapshot = _filesystem_snapshot(settings)
                        for event in _filesystem_events(settings, fs_snapshot, next_fs_snapshot):
                            print(f"Filesystem event: {event.get('metadata', {}).get('action')} {event.get('path')}", file=sys.stdout, flush=True)
                            _overmind_post_json(f"{base_url}/api/devices/{device_id}/events", event, token=token, settings=settings)
                        fs_snapshot = next_fs_snapshot
                    except Exception as error:
                        print(f"Filesystem event upload failed; continuing: {_format_overmind_error(error)}", file=sys.stderr, flush=True)

                # The procfs game monitor writes durable start/stop events to the spool.
                # Best-effort so transient failures leave those events queued for retry.
                try:
                    event_sessions, spool_files = _collect_game_event_sessions(settings, repository)
                    if event_sessions:
                        game_logs = {"type": "game_logs", "sessions": event_sessions}
                        _overmind_post_json(f"{base_url}/api/devices/{device_id}/game-logs", game_logs, token=token, settings=settings)
                        _delete_game_event_spool(spool_files)
                        _overmind_log(
                            f"Sent {len(game_logs.get('sessions') or [])} game log session(s) to Overmind"
                        )
                    else:
                        _delete_game_event_spool(spool_files)
                except Exception as error:
                    # Leave spool files in place so the next heartbeat retries them.
                    _overmind_log(f"Game log upload failed; will retry next heartbeat: {_format_overmind_error(error)}")

                # Drone/ES logs and emulator configs are no longer uploaded to Overmind --
                # Overmind neither stores nor displays them (gameplay history is still sent
                # above via /game-logs). Only device telemetry + gameplay flow to Overmind now.

                actions = response.get("actions") if isinstance(response.get("actions"), list) else None
                if actions is None:
                    legacy_action = response.get("action")
                    actions = [legacy_action] if isinstance(legacy_action, dict) else []
                actions = [action for action in actions if isinstance(action, dict)]
                if not actions:
                    time.sleep(poll_seconds)
                    continue

                claimed_names = ", ".join(str(action.get("action") or "?") for action in actions)
                _overmind_log(
                    f"Claimed {len(actions)} Overmind action(s) for {settings.overmind_device_id}: {claimed_names}",
                    also_stdout=True,
                )
                for action in actions:
                    action_name_log = str(action.get("action") or "?")
                    action_id_log = str(action.get("id") or "?")
                    payload_log = action.get("payload") if isinstance(action.get("payload"), dict) else {}
                    _overmind_log(
                        f"Executing Overmind action {action_name_log} ({action_id_log}) payload={payload_log}",
                        also_stdout=True,
                    )
                    status_value, message, result = _execute_overmind_action(settings, repository, action, config, base_url, token)
                    reboot_requested = (
                        str(action.get("action") or "").strip().lower() == "restart"
                        and status_value == "completed"
                        and not settings.use_fake_data
                    )
                    _record_processed_overmind_action(settings, action, status_value, message, result)
                    _overmind_log(
                        f"Processed Overmind action {action_name_log} ({action_id_log}): {status_value} - {message}",
                        also_stdout=True,
                    )
                    token = _report_overmind_action_completion(
                        settings,
                        repository,
                        config,
                        base_url,
                        token,
                        device_id,
                        action,
                        status_value,
                        message,
                        result,
                        integration_enabled,
                    )
                    if reboot_requested:
                        print(
                            f"Remote restart action acknowledged; exiting with code {DRONE_REMOTE_REBOOT_EXIT_CODE} for service supervisor reboot.",
                            file=sys.stdout,
                            flush=True,
                        )
                        os._exit(DRONE_REMOTE_REBOOT_EXIT_CODE)
            except (HTTPError, URLError) as error:
                print(f"Overmind action poll failed: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
            except (TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
                print(f"Overmind action poll failed: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
            except Exception as error:
                print(f"Overmind action poll unexpected error: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
            time.sleep(poll_seconds)

    thread = Thread(target=loop, name="overmind-action-poller", daemon=True)
    thread.start()


# Peer-connectivity runtime (public-IP probe + health-check thread + local-network
# workers) now lives in transfer/peer_workers.py (re-exported below).


# ROM-metadata -> Overmind sync pipeline now lives in overmind/rom_sync.py (re-exported below).
