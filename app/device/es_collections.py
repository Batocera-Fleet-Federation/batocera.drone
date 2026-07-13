"""EmulationStation game collections, systems-displayed, grouped systems, and
music volume -- read current state from es_settings.cfg and apply changes.

Extracted alongside device_control.py's screen-mode/volume helpers. Every
setting here lives in es_settings.cfg; most of them EmulationStation only
re-reads at its own startup, so applying them restarts EmulationStation (via
the same stop -> write -> overlay-save -> start sequence as
set_screen_mode.py) -- except music_volume/screensaver_minutes, confirmed on
a real device to take effect live with no restart. See set_es_collections.py
(RESTART_REQUIRED_FIELDS) for exactly which fields restart ES; it's the
canonical (and self-contained) implementation shared by the root-direct and
privileged-service-worker entry points below.
"""

import json
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

try:
    from ..common.runtime_state import _ES_LIFECYCLE_LOCK
    from ..common.settings import Settings
    from ..set_es_collections import apply_es_collections as _apply_es_collections_helper
    from .device_control import _resolve_es_systems_effective
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.runtime_state import _ES_LIFECYCLE_LOCK  # type: ignore
    from common.settings import Settings  # type: ignore
    from set_es_collections import apply_es_collections as _apply_es_collections_helper  # type: ignore
    from device.device_control import _resolve_es_systems_effective  # type: ignore


# The fixed set of EmulationStation's built-in automatic collections (name,
# display label), from batocera-emulationstation's
# CollectionSystemManager::getSystemDecls(). Excludes per-genre collections
# (dynamic -- generated from scraped genre metadata, not a fixed list) and the
# "collections" meta-entry (not itself a toggleable auto-collection).
AUTO_COLLECTION_DECLS = [
    ("all", "All Games"),
    ("recent", "Last Played"),
    ("favorites", "Favorites"),
    ("2players", "2 Players"),
    ("4players", "4 Players"),
    ("neverplayed", "Never Played"),
    ("retroachievements", "RetroAchievements"),
    ("arcade", "Arcade"),
    ("vertical", "Vertical Arcade"),
    ("lightgun", "Lightgun Games"),
    ("wheel", "Wheel Games"),
    ("trackball", "Trackball Games"),
    ("spinner", "Spinner Games"),
]

DEFAULT_MUSIC_VOLUME = 80
# EmulationStation's own default (Settings.cpp: IMPLEMENT_STATIC_INT_SETTING(ScreenSaverTime, 5 * 60 * 1000)).
DEFAULT_SCREENSAVER_MINUTES = 5
MAX_SCREENSAVER_MINUTES = 120


def _es_settings_root(settings: Settings) -> ET.Element:
    path = settings.es_settings_file
    try:
        tree = ET.parse(path) if path.exists() else ET.ElementTree(ET.Element("map"))
    except (OSError, ET.ParseError):
        tree = ET.ElementTree(ET.Element("map"))
    return tree.getroot()


def _read_typed_value(root: ET.Element, tag: str, name: str) -> Optional[str]:
    node = root.find(f".//{tag}[@name='{name}']")
    return node.get("value") if node is not None else None


def _custom_collections_dir(settings: Settings) -> Path:
    return settings.userdata_root / "system" / "configs" / "emulationstation" / "collections"


def _list_custom_collection_names(settings: Settings) -> List[str]:
    directory = _custom_collections_dir(settings)
    if not directory.is_dir():
        return []
    names = []
    for entry in directory.glob("custom-*.cfg"):
        name = entry.stem[len("custom-"):]
        if name:
            names.append(name)
    return sorted(names, key=str.lower)


def _split_list(raw: Optional[str], separator: str) -> set:
    return {part.strip() for part in str(raw or "").split(separator) if part.strip()}


def get_es_collections_state(settings: Settings) -> dict:
    """Current music volume, screensaver delay, systems-displayed, grouped-
    systems, and auto/custom collection enablement, assembled from
    es_settings.cfg + es_systems.cfg (effective, post-override) + the
    collections directory."""
    root = _es_settings_root(settings)

    music_volume_raw = _read_typed_value(root, "int", "MusicVolume")
    try:
        music_volume = max(0, min(100, int(music_volume_raw))) if music_volume_raw is not None else DEFAULT_MUSIC_VOLUME
    except (TypeError, ValueError):
        music_volume = DEFAULT_MUSIC_VOLUME

    # Stored in milliseconds (ScreenSaverTime); ES's own settings screen shows
    # and edits it in whole minutes. 0 means the screensaver is disabled.
    screensaver_raw = _read_typed_value(root, "int", "ScreenSaverTime")
    try:
        screensaver_minutes = max(0, min(MAX_SCREENSAVER_MINUTES, int(screensaver_raw) // 60000)) if screensaver_raw is not None else DEFAULT_SCREENSAVER_MINUTES
    except (TypeError, ValueError):
        screensaver_minutes = DEFAULT_SCREENSAVER_MINUTES

    hidden_names = _split_list(_read_typed_value(root, "string", "HiddenSystems"), ";")
    auto_enabled = _split_list(_read_typed_value(root, "string", "CollectionSystemsAuto"), ",")
    custom_enabled = _split_list(_read_typed_value(root, "string", "CollectionSystemsCustom"), ",")

    _, all_systems = _resolve_es_systems_effective(settings)
    # isCollection()-equivalent: es_systems.cfg entries marked hidden at the
    # definition level aren't real candidates for the display/group toggles
    # (mirrors SystemData::sSystemVector already excluding those).
    displayable = [item for item in all_systems if item.get("name") and not item.get("hidden")]

    systems = [
        {
            "name": item["name"],
            "full_name": item.get("fullname") or item["name"],
            "displayed": item["name"] not in hidden_names,
        }
        for item in displayable
    ]

    groups: Dict[str, dict] = {}
    for item in displayable:
        group_name = item.get("group")
        if not group_name:
            continue
        group_entry = groups.setdefault(group_name, {"group": group_name, "children": []})
        group_entry["children"].append({
            "name": item["name"],
            "full_name": item.get("fullname") or item["name"],
            # Grouped (folded under the parent system) unless this system was
            # individually ungrouped -- mirrors GuiCollectionSystemsOptions.cpp.
            "grouped": _read_typed_value(root, "bool", f"{item['name']}.ungroup") != "true",
        })
    group_list = sorted(groups.values(), key=lambda g: str(g["group"]).lower())
    for group_entry in group_list:
        group_entry["children"].sort(key=lambda c: c["name"].lower())

    custom_names = sorted(set(_list_custom_collection_names(settings)) | custom_enabled, key=str.lower)

    return {
        "music_volume": music_volume,
        "screensaver_minutes": screensaver_minutes,
        "systems": systems,
        "groups": group_list,
        "auto_collections": [
            {"name": name, "label": label, "enabled": name in auto_enabled}
            for name, label in AUTO_COLLECTION_DECLS
        ],
        "custom_collections": [
            {"name": name, "enabled": name in custom_enabled}
            for name in custom_names
        ],
    }


def _build_low_level_updates(settings: Settings, updates: dict) -> dict:
    """Translate the friendly partial-update shape (full desired lists) into
    the low-level es_settings.cfg field values set_es_collections.py writes."""
    low_level: dict = {}
    if "music_volume" in updates:
        try:
            low_level["music_volume"] = max(0, min(100, int(updates["music_volume"])))
        except (TypeError, ValueError):
            raise ValueError("music_volume must be a number from 0 to 100")
    if "screensaver_minutes" in updates:
        try:
            minutes = max(0, min(MAX_SCREENSAVER_MINUTES, int(updates["screensaver_minutes"])))
        except (TypeError, ValueError):
            raise ValueError(f"screensaver_minutes must be a number from 0 to {MAX_SCREENSAVER_MINUTES}")
        low_level["screensaver_time_ms"] = minutes * 60000
    if "hidden_systems" in updates:
        names = sorted({str(name).strip() for name in (updates["hidden_systems"] or []) if str(name).strip()})
        low_level["hidden_systems"] = ";".join(names)
    if "auto_collections" in updates:
        names = sorted({str(name).strip() for name in (updates["auto_collections"] or []) if str(name).strip()})
        low_level["auto_collections"] = ",".join(names)
    if "custom_collections" in updates:
        names = sorted({str(name).strip() for name in (updates["custom_collections"] or []) if str(name).strip()})
        low_level["custom_collections"] = ",".join(names)
    if "ungrouped_systems" in updates:
        desired_ungrouped = {str(name).strip() for name in (updates["ungrouped_systems"] or []) if str(name).strip()}
        _, all_systems = _resolve_es_systems_effective(settings)
        groupable = {item["name"] for item in all_systems if item.get("group") and item.get("name")}
        low_level["ungroup"] = {name: (name in desired_ungrouped) for name in groupable}
    return low_level


def apply_es_collections(settings: Settings, updates: dict) -> dict:
    """Apply a partial update (any subset of music_volume, screensaver_minutes,
    hidden_systems, ungrouped_systems, auto_collections, custom_collections --
    each a full desired list/value, not a diff). Restarts EmulationStation only
    if a systems/collections field changed; music_volume/screensaver_minutes
    apply live (see set_es_collections.RESTART_REQUIRED_FIELDS). Returns the
    freshly re-read state."""
    low_level = _build_low_level_updates(settings, updates)
    if not low_level:
        raise ValueError("No recognized fields were provided")
    if settings.use_fake_data:
        return get_es_collections_state(settings)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # Serialized against screen-mode and other ES collections changes: two
        # overlapping stop/start sequences race the same EmulationStation/X
        # session and can wedge it into a permanent crash loop.
        with _ES_LIFECYCLE_LOCK:
            _apply_es_collections_helper(low_level, config=settings.es_settings_file)
            return get_es_collections_state(settings)
    if not _request_es_collections_service_control(low_level):
        raise OSError(
            "Unable to dispatch the privileged ES collections request; the Drone service "
            "control worker may not be running."
        )
    return get_es_collections_state(settings)


def _request_es_collections_service_control(low_level_updates: dict) -> bool:
    control_dir = Path(os.environ.get("DRONE_SERVICE_CONTROL_DIR", "/userdata/system/drone-app/control"))
    request_path = control_dir / "set-es-collections.request"
    result_path = control_dir / "set-es-collections.result"
    try:
        result_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        control_dir.mkdir(parents=True, exist_ok=True)
        request_path.write_text(json.dumps(low_level_updates), encoding="utf-8")
    except OSError:
        return False
    deadline = time.monotonic() + max(3.0, float(os.environ.get("DRONE_SERVICE_CONTROL_TIMEOUT_SECONDS", "120")))
    while time.monotonic() < deadline:
        try:
            if result_path.exists():
                result = result_path.read_text(encoding="utf-8", errors="ignore").strip()
                result_path.unlink(missing_ok=True)
                if result == "ok":
                    return True
                raise OSError(result or "Privileged ES collections operation failed")
        except OSError:
            raise
        time.sleep(0.25)
    try:
        request_path.unlink(missing_ok=True)
    except OSError:
        pass
    raise OSError("Timed out waiting for the privileged ES collections service operation")
