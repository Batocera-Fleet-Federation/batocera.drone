"""Controller Assignments configuration: rules, presets, and the compiled runtime.

``config/controller-assignments.json`` is administrator content (what the
admin page edits). ``state/runtime.json`` is the compiled, self-sufficient
view the launch wrapper reads at every launch, so rule edits apply on the
next game without touching EmulationStation.

A rule is an ordered list of *selectors*; each one pulls the matching
controllers forward:

* ``{"kind": "type", "type": "gamecube"}`` -- a controller family.
* ``{"kind": "device", "name": ..., "guid": ..., "port": ...}`` -- one
  physical controller (``port`` tells identical controllers apart; blank
  matches any port).
"""

import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .launch_order import (BEST_EFFORT_MODES, CONTROLLER_TYPES, NATIVE_FAMILY_COUNT, SYSTEM_PROFILE_BY_SYSTEM,
                           SYSTEM_PROFILES, TYPE_IDS)
from .paths import ControllerAssignmentsPaths

try:
    from ..shared.files import atomic_write_json, clean_text, read_json
except ImportError:  # pragma: no cover - flat execution
    from integrations.shared.files import atomic_write_json, clean_text, read_json  # type: ignore


CONFIG_VERSION = 1
MAX_SELECTORS = 16
MAX_GAME_RULES = 2000
_SYSTEM = re.compile(r"^[A-Za-z0-9_+-]{1,64}$")
_GUID = re.compile(r"^[0-9a-fA-F]{0,32}$")
_PORT = re.compile(r"^(?:usb:\d+-\d+(?:\.\d+)*|bt:[0-9a-f:]{1,40})?$")

# Built-in presets: each switches on best-effort matching (family, analog
# stick and button fit; see ``launch_order.SYSTEM_PROFILES``) for one family
# of systems. Off until the administrator turns them on; the global
# best-effort setting covers every recognised system at once. A system or
# game rule always beats a preset.
PRESETS: Dict[str, dict] = {
    "gamecube": {"label": "GameCube", "profiles": ["gamecube"]},
    "n64": {"label": "Nintendo 64", "profiles": ["n64"]},
    "snes": {"label": "Super Nintendo & GBA / DS", "profiles": ["snes"]},
    "nes": {"label": "NES, Game Boy & PC Engine", "profiles": ["nes"]},
    "sega": {"label": "Sega (Master System to Dreamcast)", "profiles": ["sega", "saturn", "dreamcast"]},
    "playstation": {"label": "PlayStation family", "profiles": ["psx", "ps-modern"]},
    "xbox": {"label": "Xbox", "profiles": ["xbox"]},
    "arcade": {"label": "Arcade", "profiles": ["arcade"]},
    "switch": {"label": "Switch / Wii U / 3DS", "profiles": ["switch"]},
}


def preset_systems(preset_id: str) -> List[str]:
    profiles = set(PRESETS[preset_id]["profiles"])
    return sorted(system for system, profile in SYSTEM_PROFILE_BY_SYSTEM.items() if profile in profiles)


def preset_types(preset_id: str) -> List[str]:
    types: List[str] = []
    for profile in PRESETS[preset_id]["profiles"]:
        for family in SYSTEM_PROFILES[profile]["families"]:
            if family not in types:
                types.append(family)
    return types


def type_catalog() -> List[dict]:
    return [{"id": type_id, "label": label} for type_id, label in CONTROLLER_TYPES]


def preset_native_types(preset_id: str) -> List[str]:
    types: List[str] = []
    for profile in PRESETS[preset_id]["profiles"]:
        for family in SYSTEM_PROFILES[profile]["families"][:NATIVE_FAMILY_COUNT]:
            if family not in types:
                types.append(family)
    return types


def preset_catalog(enabled: Optional[dict] = None) -> List[dict]:
    enabled = enabled or {}
    return [{"id": preset_id, "label": item["label"], "types": preset_types(preset_id),
             "native_types": preset_native_types(preset_id),
             "systems": preset_systems(preset_id), "enabled": bool(enabled.get(preset_id))}
            for preset_id, item in PRESETS.items()]


def profile_catalog() -> List[dict]:
    return [{"id": profile_id, "label": item["label"], "families": list(item["families"]),
             "native": list(item["families"][:NATIVE_FAMILY_COUNT]),
             "analog": item["analog"], "buttons": item["buttons"],
             "systems": sorted(s for s, p in SYSTEM_PROFILE_BY_SYSTEM.items() if p == profile_id)}
            for profile_id, item in SYSTEM_PROFILES.items()]


def valid_system(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not _SYSTEM.fullmatch(text):
        raise ValueError("invalid system name")
    return text


def valid_game_key(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    system, _, rom = text.partition("/")
    valid_system(system)
    if (not rom or len(text) > 4096 or rom.startswith("/") or re.search(r"[\x00-\x1f\x7f]", rom)
            or ".." in rom.split("/")):
        raise ValueError("game must be <system>/<path relative to the system folder>")
    return text


def validate_selector(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("each controller rule entry must be an object")
    kind = value.get("kind")
    if kind == "type":
        if value.get("type") not in TYPE_IDS:
            raise ValueError("unknown controller type")
        return {"kind": "type", "type": value["type"]}
    if kind == "device":
        name = clean_text(value.get("name"), 128)
        guid = str(value.get("guid") or "").strip().lower()
        port = str(value.get("port") or "").strip().lower()
        if not name:
            raise ValueError("a controller entry needs a name")
        if not _GUID.fullmatch(guid):
            raise ValueError("invalid controller GUID")
        if not _PORT.fullmatch(port):
            raise ValueError("invalid controller port")
        return {"kind": "device", "name": name, "guid": guid, "port": port}
    raise ValueError("controller rule entries must be a type or a device")


def validate_order(value: Any) -> List[dict]:
    if not isinstance(value, list):
        raise ValueError("order must be a list")
    if len(value) > MAX_SELECTORS:
        raise ValueError(f"an order can list at most {MAX_SELECTORS} entries")
    order: List[dict] = []
    for item in value:
        selector = validate_selector(item)
        if selector not in order:
            order.append(selector)
    return order


def default_config() -> dict:
    return {"version": CONFIG_VERSION, "enabled": False, "auto": False, "best_effort_mode": "closest",
            "default": [], "presets": {}, "systems": {}, "games": {}}


def valid_best_effort_mode(value: Any) -> str:
    if value not in BEST_EFFORT_MODES:
        raise ValueError("best-effort mode must be one of: " + ", ".join(BEST_EFFORT_MODES))
    return value


def normalize(raw: Any) -> dict:
    """Best-effort load: drop anything invalid instead of failing the page."""
    config = default_config()
    if not isinstance(raw, dict):
        return config
    config["enabled"] = raw.get("enabled") is True
    config["auto"] = raw.get("auto") is True
    if raw.get("best_effort_mode") in BEST_EFFORT_MODES:
        config["best_effort_mode"] = raw["best_effort_mode"]
    try:
        config["default"] = validate_order(raw.get("default") or [])
    except ValueError:
        pass
    presets = raw.get("presets") if isinstance(raw.get("presets"), dict) else {}
    config["presets"] = {key: True for key, value in presets.items() if key in PRESETS and value is True}
    for bucket, key_check in (("systems", valid_system), ("games", valid_game_key)):
        source = raw.get(bucket) if isinstance(raw.get(bucket), dict) else {}
        for key, rule in source.items():
            try:
                clean_key = key_check(key)
                order = validate_order((rule or {}).get("order"))
            except (ValueError, AttributeError):
                continue
            entry = {"order": order}
            if bucket == "games":
                entry["name"] = clean_text((rule or {}).get("name"), 200)
            config[bucket][clean_key] = entry
    return config


class ConfigStore:
    def __init__(self, paths: ControllerAssignmentsPaths) -> None:
        self.paths = paths
        self._lock = threading.Lock()

    def exists(self) -> bool:
        return self.paths.config_file.is_file()

    def load(self) -> dict:
        return normalize(read_json(self.paths.config_file, None))

    def save(self, config: dict) -> dict:
        config = normalize(config)
        self.paths.ensure(self.paths.config_dir)
        atomic_write_json(self.paths.assert_owned(self.paths.config_file), config)
        return config

    def update(self, mutate) -> dict:
        with self._lock:
            config = self.load()
            mutate(config)
            return self.save(config)

    def set_enabled(self, enabled: bool) -> dict:
        return self.update(lambda config: config.__setitem__("enabled", bool(enabled)))

    def set_auto(self, enabled: Any) -> dict:
        return self.update(lambda config: config.__setitem__("auto", enabled is True))

    def set_best_effort_mode(self, mode: Any) -> dict:
        clean = valid_best_effort_mode(mode)
        return self.update(lambda config: config.__setitem__("best_effort_mode", clean))

    def set_default(self, order: Any) -> dict:
        clean = validate_order(order)
        return self.update(lambda config: config.__setitem__("default", clean))

    def set_preset(self, preset_id: str, enabled: Any) -> dict:
        if preset_id not in PRESETS:
            raise KeyError("unknown preset")

        def mutate(config: dict) -> None:
            if enabled is True:
                config["presets"][preset_id] = True
            else:
                config["presets"].pop(preset_id, None)
        return self.update(mutate)

    def set_system(self, system: Any, order: Any) -> dict:
        name = valid_system(system)
        clean = validate_order(order)
        return self.update(lambda config: config["systems"].__setitem__(name, {"order": clean}))

    def delete_system(self, system: Any) -> dict:
        name = valid_system(system)
        return self.update(lambda config: config["systems"].pop(name, None))

    def set_game(self, game: Any, order: Any, name: Any = "") -> dict:
        key = valid_game_key(game)
        clean = validate_order(order)

        def mutate(config: dict) -> None:
            if key not in config["games"] and len(config["games"]) >= MAX_GAME_RULES:
                raise ValueError(f"at most {MAX_GAME_RULES} game rules are supported")
            config["games"][key] = {"order": clean, "name": clean_text(name, 200)}
        return self.update(mutate)

    def delete_game(self, game: Any) -> dict:
        key = valid_game_key(game)
        return self.update(lambda config: config["games"].pop(key, None))


def compile_runtime(config: dict, *, es_settings_file: Path, roms_root: Path, report_file: Path) -> dict:
    """Everything the launch wrapper needs, with presets folded into systems."""
    systems: Dict[str, dict] = {}
    for preset_id in PRESETS:
        if config["presets"].get(preset_id):
            for system in preset_systems(preset_id):
                systems.setdefault(system, {"mode": "best-effort", "source": f"preset:{preset_id}"})
    for system, rule in config["systems"].items():
        systems[system] = {"order": rule["order"], "source": "system"}
    return {
        "version": CONFIG_VERSION,
        "enabled": bool(config["enabled"]),
        "auto": bool(config["auto"]),
        "best_effort_mode": config["best_effort_mode"],
        "es_settings_file": str(es_settings_file),
        "roms_root": str(roms_root),
        "report_file": str(report_file),
        "default": config["default"],
        "systems": systems,
        "games": {key: {"order": rule["order"]} for key, rule in config["games"].items()},
    }
