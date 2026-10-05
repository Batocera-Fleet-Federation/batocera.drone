"""Stream Deck configuration: schema, validation, and atomic persistence.

One JSON document (``config/streamdeck.json``) owned by the Drone process --
the worker never writes it; it consumes the compiled ``state/runtime.json``
instead. Profiles store structured, stable references only (built-in action
IDs, a game's library ID + relative ROM path fallback, script IDs); a raw
shell command is never part of a profile.

Input from the admin API is validated strictly (bad input -> ``ValueError`` ->
HTTP 400). Loading is lenient: an individually malformed button/profile/rule is
dropped with a warning, and an unreadable document is moved aside to
``streamdeck.json.broken-<ts>`` and replaced by defaults, so a corrupt file can
never wedge the integration.
"""

import copy
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

from .paths import OwnershipError, StreamDeckPaths, atomic_write_json


SCHEMA_VERSION = 1
DEFAULT_PROFILE_ID = "default"
MAX_KEYS = 64
MAX_PROFILES = 50
MAX_RULES = 50
ACTION_TYPES = ("none", "builtin", "game", "script", "profile")
IMAGE_TYPES = ("default", "game-artwork", "generated", "uploaded", "blank")
FIT_MODES = ("fill", "fit", "stretch")
TEXT_SIZES = ("small", "medium", "large")
TEXT_ALIGNMENTS = ("top", "middle", "bottom")
ARTWORK_FIELDS = ("auto", "image", "thumbnail", "marquee", "wheel", "boxart", "fanart")
PROFILE_OPERATIONS = ("next", "previous", "go-to")
RULE_EVENTS = ("game-start", "game-stop")
# Glyphs the renderer can draw (and the browser preview can mirror).
SYMBOLS = (
    "", "exit", "power", "reboot", "restart", "volume-up", "volume-down", "mute",
    "pause", "save", "load", "play", "next", "previous", "profile", "script", "game", "star",
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_HEX_ID = re.compile(r"^[a-f0-9]{32}$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def safe_id(value: Any, label: str = "id") -> str:
    normalized = str(value if value is not None else "").strip()
    if not _SAFE_ID.fullmatch(normalized) or ".." in normalized:
        raise ValueError(f"invalid {label}")
    return normalized


def hex_id(value: Any, label: str = "id") -> str:
    normalized = str(value if value is not None else "").strip().lower()
    if not _HEX_ID.fullmatch(normalized):
        raise ValueError(f"invalid {label}")
    return normalized


def new_id() -> str:
    return uuid.uuid4().hex


def clean_text(value: Any, limit: int) -> str:
    text = "".join(ch for ch in str(value if value is not None else "") if ch.isprintable())
    return text.strip()[:limit]


def _color(value: Any, default: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if not _COLOR.fullmatch(text):
        raise ValueError("colors must be #rrggbb")
    return text.lower()


def _choice(value: Any, allowed: Iterable[str], default: str, label: str) -> str:
    text = str(value if value not in (None, "") else default).strip().lower()
    if text not in allowed:
        raise ValueError(f"invalid {label}")
    return text


def _first(payload: dict, *names: str) -> Any:
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return None


def normalize_rom_path(value: Any) -> str:
    """A ROM path relative to its system directory, never absolute or escaping."""
    raw = str(value or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if raw.startswith("/") or len(raw) > 4096 or any(ord(ch) < 32 for ch in raw):
        raise ValueError("ROM path must be relative to its system directory")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts) or "\x00" in raw:
        raise ValueError("invalid ROM path")
    return "/".join(parts)


def validate_image(payload: Any) -> dict:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("button image must be an object")
    image_type = _choice(payload.get("type"), IMAGE_TYPES, "default", "image type")
    image = {
        "type": image_type,
        "fit": _choice(payload.get("fit"), FIT_MODES, "fill", "image fit mode"),
        "background": _color(payload.get("background"), "#111827"),
        "text_color": _color(_first(payload, "text_color", "textColor"), "#ffffff"),
        "text": clean_text(payload.get("text"), 24),
        "secondary_text": clean_text(_first(payload, "secondary_text", "secondaryText"), 24),
        "text_size": _choice(_first(payload, "text_size", "textSize"), TEXT_SIZES, "medium", "text size"),
        "align": _choice(payload.get("align"), TEXT_ALIGNMENTS, "middle", "text alignment"),
        "symbol": _choice(payload.get("symbol"), SYMBOLS, "", "symbol"),
        "artwork_field": _choice(_first(payload, "artwork_field", "artworkField"), ARTWORK_FIELDS, "auto", "artwork field"),
    }
    if image_type == "uploaded":
        image["image_id"] = hex_id(_first(payload, "image_id", "imageId"), "uploaded image id")
    return image


def validate_game(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Launch Game requires structured game metadata (pick a game from the library)")
    if _first(payload, "command", "cmd", "shell"):
        raise ValueError("Launch Game stores a library reference, never a command")
    return {
        "id": safe_id(payload.get("id"), "game id"),
        "name": clean_text(payload.get("name"), 200) or "Game",
        "system": safe_id(payload.get("system"), "game system"),
        "rom_path": normalize_rom_path(_first(payload, "rom_path", "romPath")),
        "metadata_source": clean_text(_first(payload, "metadata_source", "metadataSource") or "drone-rom-cache", 40),
    }


def validate_button(payload: Any, *, key: Optional[int] = None, key_count: Optional[int] = None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("button must be an object")
    raw_key = key if key is not None else payload.get("key")
    if isinstance(raw_key, (bool, float)):
        raise ValueError("button key must be an integer")
    try:
        key_index = int(raw_key)
    except (TypeError, ValueError) as error:
        raise ValueError("button key must be an integer") from error
    limit = key_count if key_count else MAX_KEYS
    if not 0 <= key_index < min(limit, MAX_KEYS):
        raise ValueError("button key is outside the device layout")
    if _first(payload, "command", "cmd", "shell"):
        raise ValueError("buttons never store shell commands; use a built-in action or a custom script")
    action_type = _choice(_first(payload, "action_type", "actionType"), ACTION_TYPES, "none", "button action type")
    button = {"key": key_index, "action_type": action_type, "label": clean_text(payload.get("label"), 40)}
    if action_type == "builtin":
        button["action_id"] = safe_id(_first(payload, "action_id", "actionId"), "built-in action id")
    elif action_type == "game":
        button["game"] = validate_game(payload.get("game"))
    elif action_type == "script":
        button["script_id"] = hex_id(_first(payload, "script_id", "scriptId"), "script id")
    elif action_type == "profile":
        operation = _choice(payload.get("operation"), PROFILE_OPERATIONS, "next", "profile navigation")
        button["operation"] = operation
        if operation == "go-to":
            button["profile_id"] = safe_id(_first(payload, "profile_id", "profileId"), "target profile id")
    button["image"] = validate_image(payload.get("image"))
    return button


def validate_rule(payload: Any, profile_ids: Iterable[str]) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("rule must be an object")
    profile_id = safe_id(_first(payload, "profile_id", "profileId"), "rule profile id")
    if profile_id not in set(profile_ids):
        raise ValueError("rule targets a profile that does not exist")
    rule_id = str(payload.get("id") or "").strip()
    return {
        "id": safe_id(rule_id, "rule id") if rule_id else new_id(),
        "enabled": bool(payload.get("enabled", True)),
        "event": _choice(payload.get("event"), RULE_EVENTS, "game-start", "rule event"),
        "system": clean_text(payload.get("system"), 64).lower(),
        "emulator": clean_text(payload.get("emulator"), 64).lower(),
        "profile_id": profile_id,
    }


def validate_settings(payload: Any, current: Optional[dict] = None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    base = dict(current or default_config()["settings"])

    def number(name: str, low: float, high: float, cast=int):
        if name not in payload:
            return base[name]
        try:
            value = cast(payload[name])
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid {name}") from error
        return min(high, max(low, value))

    return {
        "confirm_dangerous_actions": bool(payload.get("confirm_dangerous_actions", base["confirm_dangerous_actions"])),
        "hold_duration_ms": number("hold_duration_ms", 500, 5000),
        "auto_apply": bool(payload.get("auto_apply", base["auto_apply"])),
        "exit_timeout_seconds": number("exit_timeout_seconds", 5, 120),
        "launch_confirm_timeout_seconds": number("launch_confirm_timeout_seconds", 5, 180),
        "script_timeout_seconds": number("script_timeout_seconds", 1, 300),
    }


def default_config() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": False,
        "default_profile_id": DEFAULT_PROFILE_ID,
        "settings": {
            "confirm_dangerous_actions": True,
            "hold_duration_ms": 1500,
            "auto_apply": False,
            "exit_timeout_seconds": 20,
            "launch_confirm_timeout_seconds": 45,
            "script_timeout_seconds": 30,
        },
        "devices": [],
        "profiles": [{"id": DEFAULT_PROFILE_ID, "name": "Default", "buttons": []}],
        "context_rules": [],
    }


def normalize_config(payload: Any) -> Tuple[dict, List[str]]:
    """Lenient normalization for documents read from disk."""
    if not isinstance(payload, dict):
        raise ValueError("configuration must be a JSON object")
    warnings: List[str] = []
    def rows(value: Any, label: str) -> list:
        if value is None:
            return []
        if not isinstance(value, list):
            warnings.append(f"{label} must be a list; ignored")
            return []
        return value

    config = default_config()
    config["enabled"] = bool(payload.get("enabled", False))
    try:
        config["settings"] = validate_settings(payload.get("settings"), config["settings"])
    except ValueError as error:
        warnings.append(f"settings reset: {error}")

    profiles: List[dict] = []
    seen = set()
    for raw_profile in rows(payload.get("profiles"), "profiles"):
        try:
            if not isinstance(raw_profile, dict):
                raise ValueError("profile must be an object")
            profile_id = safe_id(raw_profile.get("id"), "profile id")
            if profile_id in seen:
                raise ValueError(f"duplicate profile id {profile_id}")
        except ValueError as error:
            warnings.append(f"profile dropped: {error}")
            continue
        seen.add(profile_id)
        buttons: dict = {}
        for raw_button in rows(raw_profile.get("buttons"), "buttons"):
            try:
                button = validate_button(raw_button)
            except ValueError as error:
                warnings.append(f"profile {profile_id}: button dropped: {error}")
                continue
            buttons[button["key"]] = button
        profiles.append({
            "id": profile_id,
            "name": clean_text(raw_profile.get("name"), 60) or profile_id,
            "buttons": [buttons[key] for key in sorted(buttons)],
        })
        if len(profiles) >= MAX_PROFILES:
            break
    if not profiles:
        profiles = copy.deepcopy(default_config()["profiles"])
        if payload.get("profiles"):
            warnings.append("no valid profiles; Default recreated")
    config["profiles"] = profiles
    profile_ids = [profile["id"] for profile in profiles]
    default_id = str(payload.get("default_profile_id") or "")
    config["default_profile_id"] = default_id if default_id in profile_ids else profile_ids[0]

    devices = []
    seen_devices = set()
    for raw_device in rows(payload.get("devices"), "devices"):
        try:
            device = validate_device_settings(raw_device, profile_ids)
        except ValueError as error:
            warnings.append(f"device settings dropped: {error}")
            continue
        if device["device_id"] not in seen_devices:
            seen_devices.add(device["device_id"])
            devices.append(device)
    config["devices"] = devices

    rules = []
    for raw_rule in rows(payload.get("context_rules"), "context rules")[:MAX_RULES]:
        try:
            rules.append(validate_rule(raw_rule, profile_ids))
        except ValueError as error:
            warnings.append(f"context rule dropped: {error}")
    config["context_rules"] = rules
    return config, warnings


def validate_device_settings(payload: Any, profile_ids: Iterable[str]) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("device settings must be an object")
    profile_ids = list(profile_ids)
    try:
        brightness = int(payload.get("brightness", 60))
    except (TypeError, ValueError) as error:
        raise ValueError("brightness must be an integer") from error
    startup = str(_first(payload, "startup_profile_id", "active_profile_id") or "")
    return {
        "device_id": safe_id(_first(payload, "device_id", "deviceId"), "device id"),
        "brightness": min(100, max(0, brightness)),
        "startup_profile_id": startup if startup in profile_ids else "",
    }


class ConfigStore:
    """Thread-safe load/modify/save of the configuration document."""

    def __init__(self, paths: StreamDeckPaths) -> None:
        self.paths = paths
        self._lock = threading.RLock()
        self.last_warnings: List[str] = []
        self.last_recovery: Optional[dict] = None

    @property
    def path(self) -> Path:
        return self.paths.config_file

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> dict:
        with self._lock:
            # Ownership violations are not corrupt JSON: never rename/write
            # through a redirected content directory during recovery.
            self.paths.assert_owned(self.path)
            if not self.path.exists():
                self.last_warnings = []
                return default_config()
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                config, warnings = normalize_config(payload)
                self.last_warnings = warnings
                return config
            except (OSError, ValueError, TypeError, OverflowError) as error:
                return self._recover(error)

    def _recover(self, error: Exception) -> dict:
        backup = self.path.with_name(f"{self.path.name}.broken-{int(time.time())}")
        try:
            self.path.replace(backup)
        except OSError:
            backup = None
        self.last_recovery = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": str(error)[:300],
            "backup": backup.name if backup else "",
        }
        self.last_warnings = [f"configuration was unreadable and was reset ({error})"]
        clean = default_config()
        self._write(clean)
        return clean

    def _write(self, config: dict) -> None:
        self.paths.ensure(self.paths.config_dir)
        atomic_write_json(self.paths.assert_owned(self.path), config)

    def save(self, config: dict) -> dict:
        with self._lock:
            normalized, warnings = normalize_config(config)
            if warnings:
                raise ValueError("; ".join(warnings[:3]))
            self._write(normalized)
            return normalized

    def update(self, mutate) -> Any:
        """Load, apply ``mutate(config)`` and save atomically under the store lock."""
        with self._lock:
            config = self.load()
            result = mutate(config)
            self.save(config)
            return result

    # -- profiles ---------------------------------------------------------
    @staticmethod
    def find_profile(config: dict, profile_id: str) -> dict:
        profile_id = safe_id(profile_id, "profile id")
        for profile in config["profiles"]:
            if profile["id"] == profile_id:
                return profile
        raise KeyError("profile not found")

    def create_profile(self, name: Any, duplicate_from: Optional[str] = None) -> dict:
        def mutate(config: dict) -> dict:
            if len(config["profiles"]) >= MAX_PROFILES:
                raise ValueError(f"at most {MAX_PROFILES} profiles are supported")
            buttons: list = []
            if duplicate_from:
                buttons = copy.deepcopy(self.find_profile(config, duplicate_from)["buttons"])
            profile = {"id": new_id(), "name": clean_text(name, 60) or "New Profile", "buttons": buttons}
            config["profiles"].append(profile)
            return profile
        return self.update(mutate)

    def update_profile(self, profile_id: str, payload: dict) -> dict:
        def mutate(config: dict) -> dict:
            profile = self.find_profile(config, profile_id)
            if "name" in payload:
                name = clean_text(payload.get("name"), 60)
                if not name:
                    raise ValueError("profile name is required")
                profile["name"] = name
            if payload.get("default") is True:
                config["default_profile_id"] = profile["id"]
            return profile
        return self.update(mutate)

    def delete_profile(self, profile_id: str) -> dict:
        def mutate(config: dict) -> dict:
            profile = self.find_profile(config, profile_id)
            if len(config["profiles"]) <= 1:
                raise ValueError("the only profile cannot be deleted")
            if profile["id"] == config["default_profile_id"]:
                raise ValueError("the default profile cannot be deleted; choose another default first")
            config["profiles"] = [row for row in config["profiles"] if row["id"] != profile["id"]]
            cleared = 0
            for other in config["profiles"]:
                for button in other["buttons"]:
                    if button.get("action_type") == "profile" and button.get("profile_id") == profile["id"]:
                        button.update({"action_type": "none"})
                        button.pop("operation", None)
                        button.pop("profile_id", None)
                        cleared += 1
            for device in config["devices"]:
                if device.get("startup_profile_id") == profile["id"]:
                    device["startup_profile_id"] = ""
            before = len(config["context_rules"])
            config["context_rules"] = [rule for rule in config["context_rules"] if rule["profile_id"] != profile["id"]]
            return {"deleted": profile["id"], "navigation_buttons_cleared": cleared,
                    "rules_removed": before - len(config["context_rules"])}
        return self.update(mutate)

    # -- buttons ------------------------------------------------------------
    def set_button(self, profile_id: str, key: int, payload: dict, *, key_count: Optional[int] = None) -> dict:
        button = validate_button(payload, key=key, key_count=key_count)

        def mutate(config: dict) -> dict:
            profile = self.find_profile(config, profile_id)
            if button["action_type"] == "profile" and button.get("operation") == "go-to":
                self.find_profile(config, button["profile_id"])
            profile["buttons"] = [row for row in profile["buttons"] if row["key"] != button["key"]]
            if button["action_type"] != "none" or button["image"]["type"] not in ("default", "blank") or button["label"]:
                profile["buttons"].append(button)
            profile["buttons"].sort(key=lambda row: row["key"])
            return button
        return self.update(mutate)

    # -- devices / settings / rules -----------------------------------------
    def set_device_settings(self, device_id: str, payload: dict) -> dict:
        device_id = safe_id(device_id, "device id")

        def mutate(config: dict) -> dict:
            profile_ids = [profile["id"] for profile in config["profiles"]]
            row = next((item for item in config["devices"] if item["device_id"] == device_id), None)
            merged = dict(row or {"device_id": device_id, "brightness": 60, "startup_profile_id": ""})
            for name in ("brightness", "startup_profile_id"):
                if name in payload:
                    merged[name] = payload[name]
            if merged.get("startup_profile_id") and merged["startup_profile_id"] not in profile_ids:
                raise ValueError("startup profile does not exist")
            validated = validate_device_settings(merged, profile_ids)
            config["devices"] = [item for item in config["devices"] if item["device_id"] != device_id] + [validated]
            return validated
        return self.update(mutate)

    def set_settings(self, payload: dict) -> dict:
        def mutate(config: dict) -> dict:
            config["settings"] = validate_settings(payload, config["settings"])
            return config["settings"]
        return self.update(mutate)

    def set_rules(self, rules: Any) -> list:
        if not isinstance(rules, list):
            raise ValueError("rules must be a list")
        if len(rules) > MAX_RULES:
            raise ValueError(f"at most {MAX_RULES} rules are supported")

        def mutate(config: dict) -> list:
            profile_ids = [profile["id"] for profile in config["profiles"]]
            config["context_rules"] = [validate_rule(rule, profile_ids) for rule in rules]
            return config["context_rules"]
        return self.update(mutate)

    def set_enabled(self, enabled: bool) -> dict:
        def mutate(config: dict) -> dict:
            config["enabled"] = bool(enabled)
            return config
        return self.update(mutate)


def script_references(config: dict, script_id: str) -> List[dict]:
    return [
        {"profile_id": profile["id"], "profile_name": profile["name"], "key": button["key"]}
        for profile in config["profiles"]
        for button in profile["buttons"]
        if button.get("action_type") == "script" and button.get("script_id") == script_id
    ]


def image_references(config: dict) -> set:
    return {
        button["image"]["image_id"]
        for profile in config["profiles"]
        for button in profile["buttons"]
        if button.get("image", {}).get("type") == "uploaded" and button["image"].get("image_id")
    }
