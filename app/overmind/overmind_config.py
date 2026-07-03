"""Overmind control-plane config: on-disk load/save, link-state, onboarding.

Extracted from ``drone_api.py``. Reads/writes the drone's Overmind config (token,
email, link state) in the state DB, masks secrets, normalizes link/swarm-connection
state, builds the public status payload, and assembles onboarding/registration
context. All external deps live in already-extracted modules.
"""

import hashlib
import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Optional

try:
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..transfer import local_network as _local_network
    from ..transfer.drone_network import _network_mode
    from ..transfer.drone_tls import DroneCertificateManager
    from .overmind_client import _format_overmind_error
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.drone_network import _network_mode  # type: ignore
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from overmind.overmind_client import _format_overmind_error  # type: ignore

FAKE_OVERMIND_EMAIL = "demo@example.com"
FAKE_OVERMIND_PASSWORD = "DemoPass123"
FAKE_OVERMIND_TOKEN = "demo-local-drone-token"


def overmind_config_path(settings) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / "overmind_integration.json").resolve()


def overmind_swarm_path(settings) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / "overmind_swarm.json").resolve()


def overmind_peer_results_path(settings) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / "peer_checks.json").resolve()


def overmind_load_json_file(settings, path: Path, fallback):
    return _load_state_payload(
        _state_database_path(settings.userdata_root),
        path.name,
        fallback,
        legacy_path=path,
    )


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def overmind_load_config(settings) -> dict:
    fake_email = FAKE_OVERMIND_EMAIL if settings.use_fake_data else ""
    fake_password = FAKE_OVERMIND_PASSWORD if settings.use_fake_data else ""
    fake_token = FAKE_OVERMIND_TOKEN if settings.use_fake_data else ""
    auth_token = settings.overmind_auth_token or ""
    default = {
        "overmind_url": (settings.overmind_url or "").strip(),
        "overmind_email": (fake_email if settings.use_fake_data else settings.overmind_email or "").strip(),
        "drone_name": socket.gethostname(),
        "integration_enabled": False,
        "integration_state": "not_started",
        "requested_at": None,
        "last_started_at": None,
        "last_error": None,
        "notes": "Stub integration until batocera.overmind app is available.",
    }
    if settings.overmind_password or fake_password:
        default["overmind_password"] = fake_password if settings.use_fake_data else settings.overmind_password
    if settings.overmind_token or fake_token:
        default["overmind_token"] = fake_token if settings.use_fake_data else settings.overmind_token
    if auth_token:
        default["overmind_auth_token"] = auth_token

    loaded = overmind_load_json_file(settings, overmind_config_path(settings), {})
    if not isinstance(loaded, dict) or not loaded:
        return default
    merged = dict(default)
    merged.update(loaded)
    if settings.use_fake_data:
        merged["overmind_email"] = FAKE_OVERMIND_EMAIL
        merged["overmind_password"] = FAKE_OVERMIND_PASSWORD
        merged["overmind_token"] = FAKE_OVERMIND_TOKEN
    else:
        _strip_fake_overmind_values(merged)
    return merged


def overmind_save_config(settings, payload: dict) -> None:
    _save_state_payload(
        _state_database_path(settings.userdata_root),
        overmind_config_path(settings).name,
        payload,
    )
    overmind_config_path(settings).unlink(missing_ok=True)


def overmind_public_payload(settings, config: dict) -> dict:
    config = dict(config)
    _normalize_overmind_link_state(config)
    password = str(config.get("overmind_password") or "")
    auth_token = str(config.get("overmind_auth_token") or "")
    token = str(config.get("overmind_token") or "")
    email = str(config.get("overmind_email") or "")
    state = str(config.get("integration_state") or "not_started")
    connected = bool(token) and bool(config.get("integration_enabled")) and state not in {"pending_failed", "not_started"}
    swarm_status = str(config.get("swarm_connection_status") or "")
    if state == "pending_failed" or not config.get("integration_enabled"):
        swarm_status = "disconnected"
    elif not swarm_status:
        swarm_status = "connected" if connected else ("pending approval" if state == "pending_approval" else "disconnected")
    status = {
        "configured": connected,
        "integration_enabled": bool(config.get("integration_enabled")),
        "integration_state": state,
        "swarm_connection_status": swarm_status,
        "requested_at": config.get("requested_at"),
        "last_started_at": config.get("last_started_at"),
        "last_error": config.get("last_error"),
        "last_onboarding_attempt": config.get("last_onboarding_attempt") if isinstance(config.get("last_onboarding_attempt"), dict) else None,
        "notes": config.get("notes") or "Stub integration until batocera.overmind app is available.",
    }
    return {
        "overmind_url": config.get("overmind_url") or "",
        "overmind_email": email,
        "drone_name": config.get("drone_name") or socket.gethostname(),
        "machine_id": settings.overmind_device_id,
        "password_configured": bool(password),
        "password_masked": mask_secret(password) if password else "",
        "auth_token_configured": bool(auth_token),
        "auth_token_masked": mask_secret(auth_token) if auth_token else "",
        "token_configured": bool(token),
        "token_masked": mask_secret(token) if token else "",
        "status": status,
        "swarm": overmind_load_json_file(settings, overmind_swarm_path(settings), []),
        "peer_checks": overmind_load_json_file(settings, overmind_peer_results_path(settings), []),
        "certificate": DroneCertificateManager(settings).metadata(),
    }


def build_overmind_status(settings) -> dict:
    config = overmind_load_config(settings)
    if _normalize_overmind_link_state(config):
        overmind_save_config(settings, config)
    payload = overmind_public_payload(settings, config)
    payload["network_mode"] = _network_mode(settings)
    payload["overmind_active"] = _local_network.is_overmind_mode(settings)
    if not payload["overmind_active"]:
        payload["status"]["integration_enabled"] = False
        payload["status"]["integration_state"] = "disabled"
        payload["status"]["swarm_connection_status"] = "disconnected"
    return payload


def _overmind_config_path_for_settings(settings: Settings) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / "overmind_integration.json").resolve()


def _overmind_actions_path_for_settings(settings: Settings) -> Path:
    return Path(os.environ.get(
        "OVERMIND_ACTION_LOG_FILE",
        str(settings.userdata_root / "system" / "drone-app" / "overmind_actions.log"),
    )).resolve()


def _overmind_swarm_path_for_settings(settings: Settings) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / "overmind_swarm.json").resolve()


def _overmind_peer_results_path_for_settings(settings: Settings) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / "peer_checks.json").resolve()


def _read_json_file(path: Path, fallback):
    try:
        if path.exists() and path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return fallback


def _load_overmind_config_for_settings(settings: Settings) -> dict:
    fake_email = FAKE_OVERMIND_EMAIL if settings.use_fake_data else ""
    fake_password = FAKE_OVERMIND_PASSWORD if settings.use_fake_data else ""
    fake_token = FAKE_OVERMIND_TOKEN if settings.use_fake_data else ""
    default = {
        "overmind_url": (settings.overmind_url or "").strip(),
        "overmind_email": (fake_email if settings.use_fake_data else settings.overmind_email or "").strip(),
        "drone_name": socket.gethostname(),
        "overmind_password": fake_password if settings.use_fake_data else settings.overmind_password or "",
        "overmind_auth_token": "" if settings.use_fake_data else settings.overmind_auth_token or "",
        "overmind_token": fake_token if settings.use_fake_data else settings.overmind_token or "",
        "integration_enabled": bool(settings.overmind_url and (settings.overmind_token or settings.overmind_auth_token or fake_token)),
    }
    path = _overmind_config_path_for_settings(settings)
    loaded = _load_state_payload(
        _state_database_path(settings.userdata_root),
        path.name,
        {},
        legacy_path=path,
    )
    if not isinstance(loaded, dict) or not loaded:
        return default
    merged = dict(default)
    merged.update(loaded)
    if settings.use_fake_data:
        merged["overmind_email"] = FAKE_OVERMIND_EMAIL
        merged["overmind_password"] = FAKE_OVERMIND_PASSWORD
        merged["overmind_token"] = FAKE_OVERMIND_TOKEN
    else:
        _strip_fake_overmind_values(merged)
    return merged


def _strip_fake_overmind_values(config: dict) -> None:
    """Keep previously seeded demo credentials out of real Drone state."""
    if config.get("overmind_email") == FAKE_OVERMIND_EMAIL:
        config["overmind_email"] = ""
    if config.get("overmind_password") == FAKE_OVERMIND_PASSWORD:
        config.pop("overmind_password", None)
    if config.get("overmind_token") == FAKE_OVERMIND_TOKEN:
        config.pop("overmind_token", None)
    if config.get("integration_enabled") and not (config.get("overmind_token") or config.get("overmind_auth_token")):
        config["integration_enabled"] = False
        config["integration_state"] = "not_started"


def _normalize_overmind_link_state(config: dict) -> bool:
    """Reconcile stale onboarding status once an approved Drone token exists."""
    token = str(config.get("overmind_token") or "").strip()
    enabled = bool(config.get("integration_enabled"))
    if not token or not enabled:
        return False

    state = str(config.get("integration_state") or "not_started")
    if state in {"pending_failed", "not_started", "disconnected", "disconnect_failed"}:
        return False

    changed = False
    if state in {"configured", "approval_requested", "pending_approval"}:
        config["integration_state"] = "polling"
        changed = True

    swarm_status = str(config.get("swarm_connection_status") or "")
    if swarm_status != "connected":
        config["swarm_connection_status"] = "connected"
        changed = True

    notes = str(config.get("notes") or "")
    if "Awaiting Overlord approval" in notes:
        config["notes"] = "Drone approved by Overmind and polling is active."
        changed = True

    return changed


def _mark_overmind_auth_failed(settings: Settings, config: dict, error: BaseException) -> None:
    config.pop("overmind_token", None)
    config["integration_enabled"] = False
    config["integration_state"] = "pending_failed"
    config["swarm_connection_status"] = "disconnected"
    config["last_error"] = _format_overmind_error(error)
    config["notes"] = "Overmind authorization token was rejected. Generate a new authorization token and try again."
    _save_overmind_runtime_config(settings, config)
    _save_state_payload(_state_database_path(settings.userdata_root), "overmind_swarm.json", [])
    _save_state_payload(_state_database_path(settings.userdata_root), "peer_checks.json", [])
    _overmind_swarm_path_for_settings(settings).unlink(missing_ok=True)
    _overmind_peer_results_path_for_settings(settings).unlink(missing_ok=True)


def _safe_token_fingerprint(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return "none"
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:12]


def _overmind_onboarding_context(settings: Settings, config: dict, base_url: str, payload: Optional[dict] = None) -> dict:
    email = str(config.get("overmind_email") or "").strip()
    auth_token = str(config.get("overmind_auth_token") or "").strip()
    drone_token = str(config.get("overmind_token") or "").strip()
    request_payload = payload if isinstance(payload, dict) else {}
    certificate = ((request_payload.get("batocera_info") or {}).get("certificate") or {}) if isinstance(request_payload.get("batocera_info"), dict) else {}
    return {
        "endpoint": f"{base_url.rstrip('/')}/api/devices/register" if base_url else "/api/devices/register",
        "device_id": settings.overmind_device_id,
        "drone_name": str(config.get("drone_name") or "").strip() or socket.gethostname(),
        "email_hint_present": bool(email),
        "email_hint_domain": email.split("@", 1)[1].lower() if "@" in email else "",
        "auth_token_present": bool(auth_token),
        "auth_token_fingerprint": _safe_token_fingerprint(auth_token),
        "stored_drone_token_present": bool(drone_token),
        "payload_authorization_token_present": bool(request_payload.get("authorization_token")),
        "header_authorization_token_present": bool(auth_token),
        "certificate_fingerprint_present": bool(certificate.get("fingerprint") or certificate.get("sha256_fingerprint")),
    }


def _log_overmind_onboarding(message: str, context: dict, *, error: Optional[BaseException] = None) -> None:
    safe_context = json.dumps(context, sort_keys=True)
    suffix = f" error={_format_overmind_error(error)}" if error else ""
    print(f"{message}: {safe_context}{suffix}", file=sys.stderr if error else sys.stdout, flush=True)


# Drone network-identity wrappers (_drone_network_payload, _get_local_ip_addresses,
# _drone_report_host/_reachable_url, ...) now live in transfer/drone_network.py.


# Mock-userdata detection (_mock_userdata_marker, _looks_like_pure_mock_userdata,
# _real_data_roots) now lives in common/mock_userdata.py (re-exported above).


def _save_overmind_runtime_config(settings: Settings, config: dict) -> None:
    path = _overmind_config_path_for_settings(settings)
    _save_state_payload(_state_database_path(settings.userdata_root), path.name, config)
    path.unlink(missing_ok=True)
