"""Catalog and lifecycle management for opt-in Batocera compatibility fixes."""

import hashlib
import json
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from ..common.settings import Settings
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore


LINDBERGH_INPUT_GUARD_ID = "lindbergh-input-device-guard"
SWITCH_GUI_WORKAROUND_ID = "switch-gui-autoload"
HOOK_FILENAME = "drone-lindbergh-input-device-guard.py"
ASSET_ROOT = Path(__file__).resolve().parent / "fix_assets"
ASSET_PATH = ASSET_ROOT / "lindbergh_input_guard.py"
SWITCH_LAUNCHER_ASSET = ASSET_ROOT / "switch_gui_launcher.py"
SWITCH_WRAPPER_ASSET = ASSET_ROOT / "switch_gui_autoload.sh"
SWITCH_GENERATOR_MARKER_START = "# >>> drone switch-gui-autoload fix"
SWITCH_GENERATOR_MARKER_END = "# <<< drone switch-gui-autoload fix"

FIX_CATALOG = (
    {
        "id": LINDBERGH_INPUT_GUARD_ID,
        "name": "Lindbergh input-device guard",
        "summary": "Prevents Lindbergh/LinuxLoader from crashing when too many joystick-class devices are connected.",
        "applies_to": "Sega Lindbergh (LinuxLoader)",
        "details": [
            "LinuxLoader initializes SDL joystick, haptic, and gamepad support before a game starts. On affected Batocera builds, that initialization can crash when the connected joystick topology exceeds the eight-controller range LinuxLoader expects.",
            "At a Lindbergh gameStart event, this guard inventories /sys/class/input/js*. If the total exceeds eight, it groups those entries by physical USB device, protects light guns, and temporarily unbinds only a multi-port controller adapter. Single controllers are never selected automatically.",
            "At gameStop it binds the adapter again. A detached watchdog also restores it if emulatorlauncher exits without delivering gameStop, and stale state is repaired at the next launch. A reboot restores USB binding naturally as an additional safety net.",
        ],
        "changes": [
            f"Installs /userdata/system/scripts/{HOOK_FILENAME}",
            "Stores configuration in /userdata/system/input-device-guard/config.json",
            "Writes activity to /userdata/system/logs/input-device-guard.log",
            "Does not modify Batocera's read-only system image or emulator files",
        ],
        "caution": "While a Lindbergh game is running, a selected multi-port adapter is unavailable. It is restored when the game exits.",
    },
    {
        "id": SWITCH_GUI_WORKAROUND_ID,
        "name": "Switch GUI-autoload workaround",
        "summary": "Loads selected or all Switch games through Eden/Citron's GUI after content indexing has settled.",
        "applies_to": "Nintendo Switch (Eden and Citron through RGS)",
        "details": [
            "Affected Eden and Citron builds can start a game from RGS command-line autoboot before their asynchronous game-list scan has finished rebuilding the update and DLC content provider. The result can be a permanent Launching screen, the wrong game version, missing DLC, or stalled gameplay.",
            "For games selected below, the managed RGS generator route starts the emulator without -g, waits for its GUI and game-list scan, then uses File > Load File to load the requested ROM. Games outside the selected scope keep the original -f -g command unchanged.",
            "The launcher hides most of the delay behind a temporary black cover, preserves EmulationStation's exit behavior, and falls back to normal command-line autoboot if GUI loading cannot be confirmed. The selection is read at every launch, so UI changes take effect immediately.",
        ],
        "changes": [
            "Installs a Drone-managed launcher and GUI automation wrapper beside the RGS Switch generator",
            "Adds a marked, reversible dispatch block to yuzuMainlineGenerator.py",
            "Stores all-games or selected-games scope in /userdata/system/switch-gui-workaround/config.json",
            "Keeps a pristine generator backup before the first managed patch",
        ],
        "caution": "GUI-autoload is slower than normal autoboot and depends on xdotool/wmctrl plus Eden/Citron's current keyboard shortcuts. Use it only for games that need it unless the whole library is affected.",
        "configurable": True,
    },
)


def _paths(settings: Settings) -> Dict[str, Path]:
    system_root = settings.userdata_root / "system"
    return {
        "hook": system_root / "scripts" / HOOK_FILENAME,
        "config": system_root / "input-device-guard" / "config.json",
        "log": system_root / "logs" / "input-device-guard.log",
        "switch_config": system_root / "switch-gui-workaround" / "config.json",
        "switch_backup": system_root / "backups" / "hotfixes" / "drone-switch-gui-workaround",
        "switch_generator": system_root / "rgs" / "generators" / "yuzu" / "yuzuMainlineGenerator.py",
        "switch_launcher": system_root / "rgs" / "generators" / "yuzu" / "drone-switch-gui-launcher.py",
        "switch_wrapper": system_root / "rgs" / "generators" / "yuzu" / "drone-switch-gui-autoload.sh",
    }


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fix_status(settings: Settings, metadata: Dict[str, Any]) -> Dict[str, Any]:
    paths = _paths(settings)
    if metadata["id"] == SWITCH_GUI_WORKAROUND_ID:
        return _switch_fix_status(settings, metadata)
    installed = paths["hook"].is_file()
    managed = installed and _sha256(paths["hook"]) == _sha256(ASSET_PATH)
    payload = dict(metadata)
    payload.update(
        {
            "enabled": installed,
            "managed": managed,
            "status": "enabled" if managed else ("modified" if installed else "disabled"),
            "installed_path": str(paths["hook"]),
            "log_path": str(paths["log"]),
        }
    )
    return payload


def _read_switch_config(settings: Settings) -> Dict[str, Any]:
    path = _paths(settings)["switch_config"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    scope = str(raw.get("scope") or "selected")
    if scope not in {"all", "selected"}:
        scope = "selected"
    selected = raw.get("selected_games")
    return {
        "enabled": bool(raw.get("enabled", False)),
        "scope": scope,
        "selected_games": [str(value) for value in selected] if isinstance(selected, list) else [],
    }


def _switch_game_name_map(settings: Settings) -> Dict[str, str]:
    switch_root = settings.roms_root / "switch"
    names: Dict[str, str] = {}
    gamelist = switch_root / "gamelist.xml"
    try:
        root = ET.parse(gamelist).getroot()
        for game in root.findall("game"):
            raw_path = (game.findtext("path") or "").strip().replace("\\", "/")
            if raw_path.startswith("./"):
                raw_path = raw_path[2:]
            name = (game.findtext("name") or "").strip()
            if raw_path and name:
                names[raw_path] = name
    except (OSError, ET.ParseError):
        pass
    return names


def list_switch_games(settings: Settings) -> List[Dict[str, str]]:
    switch_root = settings.roms_root / "switch"
    names = _switch_game_name_map(settings)
    extensions = {".xci", ".nsp", ".nca", ".nro"}
    games_by_path: Dict[str, Dict[str, str]] = {}
    if switch_root.is_dir():
        # Start with EmulationStation's own library so displayed names match
        # what the player sees. Reject traversal and hidden maintenance trees
        # such as .updates even if a stale gamelist entry mentions one.
        for relative, name in names.items():
            relative_path = Path(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts or any(part.startswith(".") for part in relative_path.parts):
                continue
            candidate = switch_root / relative_path
            if candidate.exists() and candidate.suffix.lower() in extensions:
                games_by_path[relative_path.as_posix()] = {
                    "id": relative_path.as_posix(),
                    "path": relative_path.as_posix(),
                    "name": name,
                }
        # Include newly copied games that ES has not written to gamelist.xml
        # yet, but never expose hidden update/DLC stores as launchable games.
        for path in sorted(switch_root.rglob("*"), key=lambda candidate: str(candidate).lower()):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            relative = path.relative_to(switch_root).as_posix()
            if any(part.startswith(".") for part in Path(relative).parts):
                continue
            games_by_path.setdefault(relative, {"id": relative, "path": relative, "name": names.get(relative) or path.stem})
    return sorted(games_by_path.values(), key=lambda game: (game["name"].casefold(), game["path"].casefold()))


def _generator_has_marker(path: Path, marker: str) -> bool:
    try:
        return marker in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _switch_fix_status(settings: Settings, metadata: Dict[str, Any]) -> Dict[str, Any]:
    paths = _paths(settings)
    config = _read_switch_config(settings)
    patched = _generator_has_marker(paths["switch_generator"], SWITCH_GENERATOR_MARKER_START)
    legacy_patched = _generator_has_marker(paths["switch_generator"], "# >>> gui-autoload hotfix")
    games = list_switch_games(settings)
    if legacy_patched and not config["selected_games"]:
        config["scope"] = "selected"
        config["selected_games"] = [game["id"] for game in games if "01006A800016E000" in game["id"].upper()]
    assets_current = (
        _sha256(paths["switch_launcher"]) == _sha256(SWITCH_LAUNCHER_ASSET)
        and _sha256(paths["switch_wrapper"]) == _sha256(SWITCH_WRAPPER_ASSET)
    )
    enabled = bool((config["enabled"] and patched) or legacy_patched)
    payload = dict(metadata)
    payload.update(
        {
            "enabled": enabled,
            "managed": enabled and patched and assets_current,
            "status": "enabled" if enabled and patched and assets_current else ("modified" if patched or legacy_patched else "disabled"),
            "scope": config["scope"],
            "selected_games": config["selected_games"],
            "games": games,
            "legacy_detected": legacy_patched,
            "installed_path": str(paths["switch_generator"]),
            "log_path": str(settings.userdata_root / "system" / "configs" / "yuzu" / "log"),
        }
    )
    return payload


def list_fixes(settings: Settings) -> List[Dict[str, Any]]:
    return [_fix_status(settings, fix) for fix in FIX_CATALOG]


def get_fix(settings: Settings, fix_id: str) -> Dict[str, Any]:
    for fix in FIX_CATALOG:
        if fix["id"] == fix_id:
            return _fix_status(settings, fix)
    raise KeyError(fix_id)


def _enable_lindbergh_input_guard(settings: Settings) -> None:
    paths = _paths(settings)
    source = ASSET_PATH.read_bytes()
    config = {
        "max_joysticks": 8,
        "protected_name_patterns": ["sinden", "light[ -]?gun"],
        "candidate_name_patterns": ["gamecube", "wup-028", "mayflash", "nintendo.*adapter"],
    }
    _atomic_write(paths["config"], (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8"), 0o644)
    _atomic_write(paths["hook"], source, 0o755)


def _disable_lindbergh_input_guard(settings: Settings) -> None:
    paths = _paths(settings)
    hook = paths["hook"]
    # Only execute the recovery action on the real Batocera userdata tree;
    # tests and desktop development roots must never manipulate host USB.
    if hook.is_file() and settings.userdata_root.resolve() == Path("/userdata"):
        try:
            subprocess.run([str(hook), "restore"], timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        hook.unlink()
    except FileNotFoundError:
        pass


def _remove_marked_block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.find(start_marker)
    if start < 0:
        return source
    line_start = source.rfind("\n", 0, start) + 1
    end = source.find(end_marker, start)
    if end < 0:
        raise OSError("managed Switch fix marker is incomplete; generator was not changed")
    line_end = source.find("\n", end)
    return source[:line_start] + source[(len(source) if line_end < 0 else line_end + 1) :]


def _remove_legacy_smash_block(source: str) -> str:
    legacy_start = source.find("# >>> gui-autoload hotfix")
    if legacy_start < 0:
        return source
    line_start = source.rfind("\n", 0, legacy_start) + 1
    legacy_end = source.find("# <<< gui-autoload hotfix", legacy_start)
    if legacy_end < 0:
        raise OSError("legacy Smash GUI-autoload marker is incomplete; generator was not changed")
    line_end = source.find("\n", legacy_end)
    return source[:line_start] + source[(len(source) if line_end < 0 else line_end + 1) :]


def _write_validated_generator(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".py", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(source)
            handle.flush()
            os.fsync(handle.fileno())
        compile(source, str(path), "exec")
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        else:
            os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _patch_switch_generator(settings: Settings) -> None:
    paths = _paths(settings)
    generator = paths["switch_generator"]
    try:
        original = generator.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise OSError(f"RGS Switch generator was not found at {generator}") from error
    source = _remove_marked_block(original, SWITCH_GENERATOR_MARKER_START, SWITCH_GENERATOR_MARKER_END)
    source = _remove_legacy_smash_block(source)
    import re

    pattern = re.compile(
        r'^(?P<indent>[ \t]*)commandArray = \["\./"\+emulator\+"\.AppImage", "-f",\s+"-g", rom \][ \t]*$',
        re.MULTILINE,
    )
    matches = list(pattern.finditer(source))
    if len(matches) != 1:
        raise OSError("RGS Switch generator layout is not recognized; no files were changed")
    match = matches[0]
    indent = match.group("indent")
    block = (
        "\n"
        + indent
        + SWITCH_GENERATOR_MARKER_START
        + ": scope is read dynamically by the managed launcher\n"
        + indent
        + "if emulator in ('eden', 'citron'):\n"
        + indent
        + "    commandArray = [\"python3\", \"/userdata/system/rgs/generators/yuzu/drone-switch-gui-launcher.py\", emulator, str(rom)]\n"
        + indent
        + SWITCH_GENERATOR_MARKER_END
    )
    patched = source[: match.end()] + block + source[match.end() :]

    backup_dir = paths["switch_backup"]
    backup_dir.mkdir(parents=True, exist_ok=True)
    # `source` has both Drone's marker and the legacy Smash-only marker
    # removed, making this a genuinely pristine rollback copy even during a
    # migration from the older bundle.
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    backup = backup_dir / f"pristine-{digest}.py"
    if not backup.exists():
        _atomic_write(backup, source.encode("utf-8"), generator.stat().st_mode & 0o777)
    _write_validated_generator(generator, patched)


def _unpatch_switch_generator(settings: Settings) -> None:
    generator = _paths(settings)["switch_generator"]
    if not generator.is_file():
        return
    original = generator.read_text(encoding="utf-8")
    updated = _remove_marked_block(original, SWITCH_GENERATOR_MARKER_START, SWITCH_GENERATOR_MARKER_END)
    updated = _remove_legacy_smash_block(updated)
    if updated != original:
        _write_validated_generator(generator, updated)


def _save_switch_config(settings: Settings, *, enabled: bool, scope: str, selected_games: List[str]) -> None:
    games = list_switch_games(settings)
    allowed = {game["id"] for game in games}
    selected = sorted({str(value) for value in selected_games if str(value) in allowed})
    names = {game["id"]: game["name"] for game in games}
    payload = {
        "enabled": bool(enabled),
        "scope": scope,
        "selected_games": selected,
        "game_names": names,
    }
    _atomic_write(
        _paths(settings)["switch_config"],
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        0o644,
    )


def _enable_switch_gui_workaround(settings: Settings, scope: str, selected_games: List[str]) -> None:
    paths = _paths(settings)
    current = _read_switch_config(settings)
    marker_complete = (
        _generator_has_marker(paths["switch_generator"], SWITCH_GENERATOR_MARKER_START)
        and _generator_has_marker(paths["switch_generator"], SWITCH_GENERATOR_MARKER_END)
    )
    # Assets land first and config remains disabled until the generator has
    # compiled successfully, so a partial failure cannot redirect launches.
    _atomic_write(paths["switch_launcher"], SWITCH_LAUNCHER_ASSET.read_bytes(), 0o755)
    _atomic_write(paths["switch_wrapper"], SWITCH_WRAPPER_ASSET.read_bytes(), 0o755)
    _save_switch_config(settings, enabled=False, scope=scope, selected_games=selected_games)
    # Selection-only changes are config writes. Re-patch only on first enable,
    # migration, repair, or re-enable after a clean disable.
    if not (current["enabled"] and marker_complete):
        _patch_switch_generator(settings)
    _save_switch_config(settings, enabled=True, scope=scope, selected_games=selected_games)


def _disable_switch_gui_workaround(settings: Settings, scope: str, selected_games: List[str]) -> None:
    # Disable dispatch before editing the generator. If generator validation
    # fails, launches still fall through to ordinary -f -g behavior.
    _save_switch_config(
        settings,
        enabled=False,
        scope=scope,
        selected_games=selected_games,
    )
    _unpatch_switch_generator(settings)


def set_fix_enabled(
    settings: Settings,
    fix_id: str,
    enabled: bool,
    *,
    scope: str = "selected",
    selected_games: Optional[List[str]] = None,
) -> Dict[str, Any]:
    selected_games = selected_games if isinstance(selected_games, list) else []
    if scope not in {"all", "selected"}:
        raise ValueError("scope must be 'all' or 'selected'")
    if fix_id == LINDBERGH_INPUT_GUARD_ID:
        if enabled:
            _enable_lindbergh_input_guard(settings)
        else:
            _disable_lindbergh_input_guard(settings)
    elif fix_id == SWITCH_GUI_WORKAROUND_ID:
        if enabled:
            _enable_switch_gui_workaround(settings, scope, selected_games)
        else:
            _disable_switch_gui_workaround(settings, scope, selected_games)
    else:
        raise KeyError(fix_id)
    return get_fix(settings, fix_id)
