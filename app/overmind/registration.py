"""Overmind token registration + action-result reporting.

Extracted from ``drone_api.py``. Registers/claims the drone's Overmind token (with
the network payload, cert metadata and system-info snapshot), reclaims it after a
401, records processed actions, reports action completion, and summarizes results.
"""

import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError
from urllib.parse import quote

try:
    from ..common.logging_setup import _overmind_log
    from ..common.settings import Settings
    from ..storage.state_store import append_event as _append_state_event
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_events as _load_state_events
    from ..transfer import local_network as _local_network
    from ..transfer.drone_network import (
        _drone_advertised_api_port,
        _drone_network_payload,
        _drone_reachable_url,
        _drone_report_host,
    )
    from ..transfer.drone_tls import DroneCertificateManager
    from ..transfer.network_identity import drone_scheme as _drone_scheme
    from .overmind_client import _format_overmind_error, _overmind_post_json
    from .overmind_config import (
        _log_overmind_onboarding,
        _mark_overmind_auth_failed,
        _overmind_actions_path_for_settings,
        _overmind_onboarding_context,
        _save_overmind_runtime_config,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.logging_setup import _overmind_log  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage.state_store import append_event as _append_state_event  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_events as _load_state_events  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.drone_network import (  # type: ignore
        _drone_advertised_api_port,
        _drone_network_payload,
        _drone_reachable_url,
        _drone_report_host,
    )
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from transfer.network_identity import drone_scheme as _drone_scheme  # type: ignore
    from overmind.overmind_client import _format_overmind_error, _overmind_post_json  # type: ignore
    from overmind.overmind_config import (  # type: ignore
        _log_overmind_onboarding,
        _mark_overmind_auth_failed,
        _overmind_actions_path_for_settings,
        _overmind_onboarding_context,
        _save_overmind_runtime_config,
    )


def _summarize_overmind_result(result: Optional[dict]) -> str:
    if not isinstance(result, dict):
        return ""
    if result.get("type") in {"rom_metadata", "asset_metadata"}:
        return (
            f"{len(result.get('systems') or [])} systems, {len(result.get('roms') or [])} ROMs, "
            f"{len(result.get('bios') or [])} BIOS files, {len(result.get('artwork') or [])} artwork rows, "
            f"{len(result.get('gamelists') or [])} gamelists"
        )
    if result.get("type") == "game_logs":
        return f"{len(result.get('sessions') or [])} parsed sessions, {len(result.get('logs') or [])} logs"
    if result.get("type") == "emulator_configs":
        return f"{len(result.get('configs') or [])} config files"
    if result.get("type") == "log_sources":
        return f"{len(result.get('logs') or [])} log sources"
    return "data returned"


def _record_processed_overmind_action(
    settings: Settings,
    action: dict,
    status_value: str,
    message: str,
    result: Optional[dict] = None,
) -> None:
    entry = {
            "id": action.get("id"),
            "device_id": settings.overmind_device_id,
            "action": action.get("action"),
            "status": status_value,
            "message": message,
            "result_summary": _summarize_overmind_result(result),
            "processed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "fake_data": settings.use_fake_data,
    }
    legacy_path = _overmind_actions_path_for_settings(settings)
    _load_state_events(
        _state_database_path(settings.userdata_root),
        "overmind_actions",
        legacy_path=legacy_path,
    )
    _append_state_event(
        _state_database_path(settings.userdata_root),
        "overmind_actions",
        entry,
        max_events=500,
    )


# _network_mode + _certificate_pem_fingerprint now live in transfer/drone_network.py.


def _register_or_claim_overmind_token(settings: Settings, repository: "RomRepository", config: dict, base_url: str) -> Optional[str]:
    # _collect_system_info_payload lives in device/system_info.py; lazy-import to avoid a cycle.
    try:
        from ..device.system_info import _collect_system_info_payload
    except ImportError:  # pragma: no cover - flat execution
        from device.system_info import _collect_system_info_payload  # type: ignore
    auth_token = str(config.get("overmind_auth_token") or "").strip()
    email = str(config.get("overmind_email") or "").strip()
    drone_name = str(config.get("drone_name") or "").strip() or socket.gethostname()
    network = _drone_network_payload(settings)
    reachable_url = _drone_reachable_url(settings, network)
    payload = {
        "device_id": settings.overmind_device_id,
        "device_name": drone_name,
        "api_port": _drone_advertised_api_port(settings),
        "scheme": _drone_scheme(settings),
        "reachable_url": reachable_url,
        "batocera_info": {
            "model": "Batocera Drone",
            "system": sys.platform,
            "architecture": os.uname().machine if hasattr(os, "uname") else "",
            "cpu_model": os.environ.get("DRONE_CPU_MODEL", "unknown"),
            "cpu_cores": os.cpu_count() or 1,
            "cpu_threads": os.cpu_count() or 1,
            "cpu_max_frequency": "unknown",
            "memory_available": "unknown",
            "memory_total": "unknown",
            "ip_address": _drone_report_host(settings, network),
            "network": network,
            "api_port": _drone_advertised_api_port(settings),
            "scheme": _drone_scheme(settings),
            "reachable_url": reachable_url,
            "system_info": _collect_system_info_payload(settings),
            "certificate": DroneCertificateManager(settings).metadata(),
        },
    }
    if email:
        payload["email"] = email
    if auth_token:
        payload["authorization_token"] = auth_token
    context = _overmind_onboarding_context(settings, config, base_url, payload)
    config["last_onboarding_attempt"] = context
    _log_overmind_onboarding("Overmind onboarding request prepared", context)
    try:
        response = _overmind_post_json(f"{base_url}/api/devices/register", payload, token=auth_token or None, settings=settings)
    except Exception as error:
        _mark_overmind_auth_failed(settings, config, error)
        config["last_onboarding_attempt"] = context
        _save_overmind_runtime_config(settings, config)
        _log_overmind_onboarding("Overmind onboarding request failed", context, error=error)
        return None
    config["last_onboarding_attempt"] = context
    if response.get("drone_token"):
        config["overmind_token"] = str(response["drone_token"])
        config["integration_enabled"] = True
        config["integration_state"] = "polling"
        config["swarm_connection_status"] = "connected"
        config["last_error"] = None
        config["notes"] = "Drone approved by Overmind and polling is active."
        _save_overmind_runtime_config(settings, config)
        print(f"Overmind onboarding approved for {settings.overmind_device_id}", file=sys.stdout, flush=True)
        return config["overmind_token"]
    config["integration_state"] = "pending_approval"
    config["swarm_connection_status"] = "pending approval"
    config["integration_enabled"] = True
    config["notes"] = response.get("message") or "Psionic connection detected. Awaiting Overlord approval."
    config["last_error"] = None
    _save_overmind_runtime_config(settings, config)
    _log_overmind_onboarding("Overmind onboarding request pending approval", context)
    return None


def _reclaim_overmind_token_after_unauthorized(settings: Settings, repository: "RomRepository", config: dict, base_url: str, error: HTTPError) -> Optional[str]:
    auth_token = str(config.get("overmind_auth_token") or "").strip()
    if not auth_token:
        return None
    config.pop("overmind_token", None)
    config["integration_state"] = "credential_reclaim"
    config["last_error"] = _format_overmind_error(error)
    config["notes"] = "Stored Drone bearer token was rejected; reclaiming with bound authorization token."
    _save_overmind_runtime_config(settings, config)
    _overmind_log(
        f"Overmind bearer token rejected for {settings.overmind_device_id}; reclaiming with bound authorization token."
    )
    return _register_or_claim_overmind_token(settings, repository, config, base_url)


def _report_overmind_action_completion(
    settings: "Settings",
    repository: "RomRepository",
    config: dict,
    base_url: str,
    token: str,
    device_id: str,
    action: dict,
    status_value: str,
    message: str,
    result: Optional[dict],
    integration_enabled: bool,
) -> str:
    """Report an action's completion to Overmind, returning the (possibly reclaimed) token.

    If the stored bearer token was rotated out from under us, the completion POST gets a
    401. Without recovery the action would stay 'in_progress' in Overmind forever (the
    Drone already executed and dropped it). So on 401 we reclaim the token with the bound
    authorization token and retry the completion once, mirroring the heartbeat path.
    """
    if not _local_network.is_overmind_mode(settings):
        return token
    action_id = quote(str(action.get("id") or ""), safe="")
    action_label = str(action.get("action") or "?")
    action_id_log = str(action.get("id") or "?")
    if not action_id:
        return token
    complete_url = f"{base_url}/api/devices/{device_id}/actions/{action_id}/complete"
    completion_payload: dict = {"status": status_value, "message": message}
    if result is not None:
        completion_payload["result"] = result
    try:
        try:
            _overmind_post_json(complete_url, completion_payload, token=token, settings=settings)
        except HTTPError as error:
            if error.code == 401 and integration_enabled:
                replacement_token = _reclaim_overmind_token_after_unauthorized(settings, repository, config, base_url, error)
                if not replacement_token:
                    raise
                token = replacement_token
                _overmind_post_json(complete_url, completion_payload, token=token, settings=settings)
            else:
                raise
        _overmind_log(
            f"Reported Overmind action completion {action_label} ({action_id_log}): {status_value}"
        )
    except Exception as error:
        _overmind_log(
            f"Failed to report Overmind action completion {action_id_log}: {_format_overmind_error(error)}",
            also_stdout=True,
        )
    return token
