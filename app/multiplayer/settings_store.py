"""Multiplayer settings: validated, bounded, persisted in the Drone state DB.

Defaults are conservative: invitations never auto-accept and a launch always
needs the local user's confirmation. Unknown keys are rejected so a typo cannot
silently disable a safeguard.
"""

import re
from typing import Any, Dict

from .models import MECHANISMS, MultiplayerError, clean_bool, clean_int, clean_peer_id, require_dict
from .persistence import Document

TRANSPORTS = ("auto", "lan", "tailnet")
INVITE_PERMISSIONS = ("all_paired", "allowlist", "none")
LAUNCH_AUTHORIZATION = ("confirm", "auto")
CONTROLLER_AUTHORIZATION = ("host_assigned", "disabled")
RESOLUTIONS = ("854x480", "1280x720", "1920x1080", "2560x1440", "3840x2160")
QUALITY_PRESETS = ("low_latency", "balanced", "quality")
ENCODING_PREFERENCES = ("auto", "hardware", "software")
RELAY_PROVIDERS = ("none",)
_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

DEFAULTS = {
    "general": {
        "enabled": True,
        "invitations_enabled": True,
        "auto_accept_invitations": False,
        "preferred_mechanism": "",
        "max_simultaneous_sessions": 1,
        "session_timeout_minutes": 240,
        "invitation_ttl_minutes": 15,
        "auto_restore_configuration": True,
    },
    "networking": {
        "transport": "auto",
        "prefer_tailscale": False,
        "relay_provider": "none",
        "port_range_start": 55435,
        "port_range_end": 55535,
    },
    "emulators": {
        "disabled_adapters": [],
        "core_overrides": {},
    },
    "streaming": {
        "enabled": True,
        "quality": "balanced",
        "max_resolution": "1280x720",
        "max_fps": 60,
        "max_bitrate_kbps": 10000,
        "encoding": "auto",
    },
    "privacy": {
        "invite_permission": "all_paired",
        "allowed_inviters": [],
        "local_launch_authorization": "confirm",
        "remote_controller_authorization": "host_assigned",
        "session_logging": True,
    },
}


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    return value


def _choice(value: Any, name: str, allowed) -> str:
    text = str(value if value is not None else "").strip().lower()
    if text not in allowed:
        raise MultiplayerError("invalid_setting", f"{name} must be one of: {', '.join(allowed)}")
    return text


def _validate_general(section: dict) -> dict:
    out = dict(section)
    for key in ("enabled", "invitations_enabled", "auto_accept_invitations", "auto_restore_configuration"):
        if key in out:
            out[key] = clean_bool(out[key], key)
    if "preferred_mechanism" in out:
        text = str(out["preferred_mechanism"] or "").strip().upper()
        if text and text not in MECHANISMS:
            raise MultiplayerError("invalid_setting", f"preferred_mechanism must be empty or one of: {', '.join(MECHANISMS)}")
        out["preferred_mechanism"] = text
    if "max_simultaneous_sessions" in out:
        out["max_simultaneous_sessions"] = clean_int(out["max_simultaneous_sessions"], "max_simultaneous_sessions", 1, 4)
    if "session_timeout_minutes" in out:
        out["session_timeout_minutes"] = clean_int(out["session_timeout_minutes"], "session_timeout_minutes", 5, 1440)
    if "invitation_ttl_minutes" in out:
        out["invitation_ttl_minutes"] = clean_int(out["invitation_ttl_minutes"], "invitation_ttl_minutes", 1, 240)
    return out


def _validate_networking(section: dict) -> dict:
    out = dict(section)
    if "transport" in out:
        out["transport"] = _choice(out["transport"], "transport", TRANSPORTS)
    if "prefer_tailscale" in out:
        out["prefer_tailscale"] = clean_bool(out["prefer_tailscale"], "prefer_tailscale")
    if "relay_provider" in out:
        out["relay_provider"] = _choice(out["relay_provider"], "relay_provider", RELAY_PROVIDERS)
    for key in ("port_range_start", "port_range_end"):
        if key in out:
            out[key] = clean_int(out[key], key, 1024, 65535)
    return out


def _validate_emulators(section: dict) -> dict:
    out = dict(section)
    if "disabled_adapters" in out:
        raw = out["disabled_adapters"]
        if not isinstance(raw, list) or len(raw) > 64:
            raise MultiplayerError("invalid_setting", "disabled_adapters must be a list of adapter ids")
        cleaned = []
        for item in raw:
            text = str(item or "").strip().lower()
            if not _TOKEN.fullmatch(text):
                raise MultiplayerError("invalid_setting", "disabled_adapters contains an invalid adapter id")
            if text not in cleaned:
                cleaned.append(text)
        out["disabled_adapters"] = cleaned
    if "core_overrides" in out:
        raw = out["core_overrides"]
        if not isinstance(raw, dict) or len(raw) > 128:
            raise MultiplayerError("invalid_setting", "core_overrides must map system names to core names")
        cleaned_map: Dict[str, str] = {}
        for system, core in raw.items():
            system_text, core_text = str(system).strip().lower(), str(core or "").strip().lower()
            if not _TOKEN.fullmatch(system_text) or (core_text and not _TOKEN.fullmatch(core_text)):
                raise MultiplayerError("invalid_setting", "core_overrides contains an invalid system or core name")
            if core_text:
                cleaned_map[system_text] = core_text
        out["core_overrides"] = cleaned_map
    return out


def _validate_streaming(section: dict) -> dict:
    out = dict(section)
    if "enabled" in out:
        out["enabled"] = clean_bool(out["enabled"], "streaming.enabled")
    if "quality" in out:
        out["quality"] = _choice(out["quality"], "quality", QUALITY_PRESETS)
    if "max_resolution" in out:
        out["max_resolution"] = _choice(out["max_resolution"], "max_resolution", RESOLUTIONS)
    if "max_fps" in out:
        out["max_fps"] = clean_int(out["max_fps"], "max_fps", 15, 120)
    if "max_bitrate_kbps" in out:
        out["max_bitrate_kbps"] = clean_int(out["max_bitrate_kbps"], "max_bitrate_kbps", 500, 150000)
    if "encoding" in out:
        out["encoding"] = _choice(out["encoding"], "encoding", ENCODING_PREFERENCES)
    return out


def _validate_privacy(section: dict) -> dict:
    out = dict(section)
    if "invite_permission" in out:
        out["invite_permission"] = _choice(out["invite_permission"], "invite_permission", INVITE_PERMISSIONS)
    if "allowed_inviters" in out:
        raw = out["allowed_inviters"]
        if not isinstance(raw, list) or len(raw) > 256:
            raise MultiplayerError("invalid_setting", "allowed_inviters must be a list of Drone ids")
        out["allowed_inviters"] = sorted({clean_peer_id(item, "allowed inviter") for item in raw})
    if "local_launch_authorization" in out:
        out["local_launch_authorization"] = _choice(out["local_launch_authorization"], "local_launch_authorization", LAUNCH_AUTHORIZATION)
    if "remote_controller_authorization" in out:
        out["remote_controller_authorization"] = _choice(out["remote_controller_authorization"], "remote_controller_authorization", CONTROLLER_AUTHORIZATION)
    if "session_logging" in out:
        out["session_logging"] = clean_bool(out["session_logging"], "session_logging")
    return out


_VALIDATORS = {
    "general": _validate_general,
    "networking": _validate_networking,
    "emulators": _validate_emulators,
    "streaming": _validate_streaming,
    "privacy": _validate_privacy,
}


class MultiplayerSettings:
    def __init__(self, settings: Any) -> None:
        self._doc = Document(settings, "settings", dict)
        # Execution takes the session lock first, then this lock. Settings writes
        # never take session locks, so revocation and resource acquisition serialize.
        self.launch_lock = self._doc.lock

    def launch_authority(self) -> dict:
        """Local revocation generations, persisted but never accepted as settings input."""
        stored = self._doc.load()
        return {key: stored.get("_launch_authority", {}).get(key, 0)
                for key in ("enabled", "automatic")}

    def get(self) -> dict:
        stored = self._doc.load()
        merged = _copy(DEFAULTS)
        for section, values in merged.items():
            saved = stored.get(section)
            if isinstance(saved, dict):
                for key in values:
                    if key in saved:
                        values[key] = _copy(saved[key])
        return merged

    def update(self, patch: Any) -> dict:
        with self.launch_lock:
            return self._update(patch)

    def _update(self, patch: Any) -> dict:
        patch = require_dict(patch, "settings")
        unknown = sorted(set(patch) - set(DEFAULTS))
        if unknown:
            raise MultiplayerError("invalid_setting", f"unknown settings section: {unknown[0]}")
        cleaned: Dict[str, dict] = {}
        for section, values in patch.items():
            values = require_dict(values, f"settings.{section}")
            bad = sorted(set(values) - set(DEFAULTS[section]))
            if bad:
                raise MultiplayerError("invalid_setting", f"unknown setting: {section}.{bad[0]}")
            cleaned[section] = _VALIDATORS[section](values)
        previous = self.get()
        merged = _copy(previous)
        for section, values in cleaned.items():
            merged[section].update(values)
        if merged["networking"]["port_range_end"] < merged["networking"]["port_range_start"]:
            raise MultiplayerError("invalid_setting", "port_range_end must not be below port_range_start")
        authority = self.launch_authority()
        if previous["general"]["enabled"] and not merged["general"]["enabled"]:
            authority["enabled"] += 1
        if (previous["privacy"]["local_launch_authorization"] == "auto" and
                merged["privacy"]["local_launch_authorization"] != "auto"):
            authority["automatic"] += 1
        self._doc.save({**merged, "_launch_authority": authority})
        return merged
