"""Media Keys configuration: managed keys, bindings, and strict validation.

A binding stores a *typed reference* -- a built-in catalog ID or a script ID --
never a command, so editing config can only select reviewed actions or
administrator scripts. The press-time dispatcher reads this file on every key
press, which is why binding/script edits apply without touching hotkeygen.
"""

import copy
import threading
import time
from typing import Any, Dict

from .paths import MediaKeysPaths

try:
    from ..shared.batocera_control import BUILTIN_COMMANDS
    from ..shared.files import atomic_write_json, hex_id, read_json
except ImportError:  # pragma: no cover - flat execution
    from integrations.shared.batocera_control import BUILTIN_COMMANDS  # type: ignore
    from integrations.shared.files import atomic_write_json, hex_id, read_json  # type: ignore


CONFIG_VERSION = 1
DEFAULT_HOLD_SECONDS = 3.0
MIN_CONFIRM_HOLD_SECONDS = 2.0
MAX_HOLD_SECONDS = 10.0

# binding id -> (Linux input key name hotkeygen reports, label)
MANAGED_KEYS: Dict[str, tuple] = {
    "volume-up": ("KEY_VOLUMEUP", "Volume Up"),
    "volume-down": ("KEY_VOLUMEDOWN", "Volume Down"),
    "mute": ("KEY_MUTE", "Mute"),
    "exit": ("KEY_EXIT", "Exit"),
    "reboot": ("KEY_RESTART", "Reboot"),
}

DEFAULT_BINDINGS = {
    "volume-up": {"action_type": "builtin", "action_id": "volume-up"},
    "volume-down": {"action_type": "builtin", "action_id": "volume-down"},
    "mute": {"action_type": "builtin", "action_id": "volume-mute"},
    "exit": {"action_type": "builtin", "action_id": "exit-game"},
    "reboot": {"action_type": "builtin", "action_id": "reboot", "hold_seconds": DEFAULT_HOLD_SECONDS},
}


def default_config() -> dict:
    return {"version": CONFIG_VERSION, "enabled": False, "bindings": copy.deepcopy(DEFAULT_BINDINGS)}


def builtin_catalog() -> list:
    return [{"id": action_id, "label": label, "requires_hold": confirm}
            for action_id, (label, _call, confirm) in BUILTIN_COMMANDS.items()]


def requires_hold(binding: dict) -> bool:
    """Reboot/shutdown always need a hold; any binding may opt in with ``hold_seconds``."""
    if binding.get("action_type") == "builtin":
        entry = BUILTIN_COMMANDS.get(binding.get("action_id"))
        if entry and entry[2]:
            return True
    return float(binding.get("hold_seconds") or 0) > 0


def hold_seconds(binding: dict) -> float:
    value = float(binding.get("hold_seconds") or 0)
    entry = BUILTIN_COMMANDS.get(binding.get("action_id")) if binding.get("action_type") == "builtin" else None
    if entry and entry[2]:
        return max(MIN_CONFIRM_HOLD_SECONDS, value or DEFAULT_HOLD_SECONDS)
    return value


def validate_binding(binding_id: str, raw: Any) -> dict:
    if binding_id not in MANAGED_KEYS:
        raise ValueError(f"unknown key binding: {binding_id}")
    if not isinstance(raw, dict):
        raise ValueError("binding must be an object")
    action_type = raw.get("action_type")
    if action_type == "none":
        return {"action_type": "none"}
    if action_type == "builtin":
        action_id = raw.get("action_id")
        if not isinstance(action_id, str) or action_id not in BUILTIN_COMMANDS:
            raise ValueError("unknown built-in action")
        binding = {"action_type": "builtin", "action_id": action_id}
    elif action_type == "script":
        binding = {"action_type": "script", "script_id": hex_id(raw.get("script_id"), "script id")}
    else:
        raise ValueError("action_type must be builtin, script or none")
    hold = raw.get("hold_seconds", 0)
    if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not 0 <= hold <= MAX_HOLD_SECONDS:
        raise ValueError(f"hold_seconds must be between 0 and {MAX_HOLD_SECONDS:g}")
    if hold or (action_type == "builtin" and BUILTIN_COMMANDS[binding["action_id"]][2]):
        binding["hold_seconds"] = float(hold) if hold else DEFAULT_HOLD_SECONDS
        if action_type == "builtin" and BUILTIN_COMMANDS[binding["action_id"]][2]:
            binding["hold_seconds"] = max(MIN_CONFIRM_HOLD_SECONDS, binding["hold_seconds"])
    return binding


def normalize_config(raw: Any) -> dict:
    """Tolerant load: invalid or missing entries fall back to the defaults."""
    config = default_config()
    if not isinstance(raw, dict):
        return config
    config["enabled"] = raw.get("enabled") is True
    bindings = raw.get("bindings")
    if isinstance(bindings, dict):
        for binding_id in MANAGED_KEYS:
            if binding_id in bindings:
                try:
                    config["bindings"][binding_id] = validate_binding(binding_id, bindings[binding_id])
                except ValueError:
                    pass
    return config


class ConfigStore:
    def __init__(self, paths: MediaKeysPaths) -> None:
        self.paths = paths
        self._lock = threading.Lock()

    def exists(self) -> bool:
        return self.paths.config_file.is_file() and not self.paths.config_file.is_symlink()

    def load(self) -> dict:
        return normalize_config(read_json(self.paths.config_file, None))

    def _save(self, config: dict) -> dict:
        self.paths.ensure(self.paths.config_dir)
        config["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        atomic_write_json(self.paths.assert_owned(self.paths.config_file), config)
        return config

    def set_enabled(self, enabled: bool) -> dict:
        with self._lock:
            config = self.load()
            config["enabled"] = bool(enabled)
            return self._save(config)

    def set_binding(self, binding_id: str, raw: Any) -> dict:
        binding = validate_binding(binding_id, raw)
        with self._lock:
            config = self.load()
            config["bindings"][binding_id] = binding
            return self._save(config)

    def script_references(self, script_id: str) -> list:
        return [{"binding_id": binding_id, "label": MANAGED_KEYS[binding_id][1]}
                for binding_id, binding in self.load()["bindings"].items()
                if binding.get("action_type") == "script" and binding.get("script_id") == script_id]
