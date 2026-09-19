"""Safe EmulationStation projection for read-only network ROM references.

The NFS/SMB mount stays below Drone's private state directory.  EmulationStation
is pointed at selected remote systems through one Drone-owned es_systems overlay;
``/userdata/roms`` is never renamed, replaced, mounted over, or linked into.
"""

from __future__ import annotations

import copy
import os
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

try:
    from ..common.settings import Settings
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore


# Sort after Batocera's ordinary per-system overlays (for example
# es_systems_steam.cfg), so this temporary path override is the final writer.
OVERLAY_FILENAME = "es_systems_zz_drone_network.cfg"
OVERLAY_OWNER_ATTRIBUTE = "data-drone-owned"
OVERLAY_OWNER_VALUE = "network-reference"
PARSE_GAMELIST_SETTING = "ParseGamelistOnly"


def overlay_path(settings: Settings) -> Path:
    return settings.userdata_root / "system" / "configs" / "emulationstation" / OVERLAY_FILENAME


def _atomic_write_xml(path: Path, tree: ET.ElementTree) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tree.write(temporary, encoding="utf-8", xml_declaration=True)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _parse_system_nodes(path: Path) -> list[ET.Element]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return []
    return [node for node in root.findall(".//system") if str(node.findtext("name") or "").strip()]


def _effective_system_nodes(settings: Settings) -> dict[str, ET.Element]:
    config_dir = settings.userdata_root / "system" / "configs" / "emulationstation"
    override = config_dir / "es_systems.cfg"
    source = override if override.is_file() else settings.es_systems_file
    by_name = {
        str(node.findtext("name") or "").strip(): copy.deepcopy(node)
        for node in _parse_system_nodes(source)
    }
    try:
        overlays = sorted(config_dir.glob("es_systems_*.cfg"), key=lambda item: item.name.lower())
    except OSError:
        overlays = []
    owned_overlay = overlay_path(settings)
    for candidate in overlays:
        if candidate == owned_overlay or not candidate.is_file():
            continue
        for node in _parse_system_nodes(candidate):
            by_name[str(node.findtext("name") or "").strip()] = copy.deepcopy(node)
    return by_name


def _validated_game_paths(system_dir: Path) -> list[Path]:
    gamelist = system_dir / "gamelist.xml"
    try:
        root = ET.parse(gamelist).getroot()
    except FileNotFoundError as error:
        raise ValueError(f"{system_dir.name}: gamelist.xml is required for a safe network reference") from error
    except (OSError, ET.ParseError) as error:
        raise ValueError(f"{system_dir.name}: gamelist.xml is not readable and valid: {error}") from error

    paths: list[Path] = []
    invalid: list[str] = []
    for game in root.findall("game"):
        raw = str(game.findtext("path") or "").strip().replace("\\", "/")
        relative = PurePosixPath(raw)
        if not raw or relative.is_absolute() or ".." in relative.parts:
            invalid.append(raw or "<empty>")
            continue
        parts = [part for part in relative.parts if part not in {"", "."}]
        if not parts:
            invalid.append(raw)
            continue
        paths.append(system_dir.joinpath(*parts))
    if invalid:
        raise ValueError(f"{system_dir.name}: gamelist.xml contains unsafe game paths ({', '.join(invalid[:3])})")
    if not paths:
        raise ValueError(f"{system_dir.name}: gamelist.xml contains no games")

    # Network latency, not file count, dominates these checks. A small pool
    # verifies the authoritative gamelist without serializing hundreds of NFS
    # GETATTR round trips (and without recursively walking the game trees).
    workers = min(16, len(paths))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="network-rom-preflight") as pool:
        present = list(pool.map(lambda candidate: candidate.exists(), paths))
    missing = [path for path, exists in zip(paths, present) if not exists]
    if missing:
        preview = ", ".join(str(path.relative_to(system_dir)) for path in missing[:3])
        raise ValueError(f"{system_dir.name}: gamelist references {len(missing)} missing game path(s) ({preview})")
    return paths


def _build_overlay(settings: Settings, mount_point: Path, system_names: Iterable[str]) -> tuple[ET.ElementTree, list[dict]]:
    definitions = _effective_system_nodes(settings)
    root = ET.Element("systemList", {OVERLAY_OWNER_ATTRIBUTE: OVERLAY_OWNER_VALUE})
    rows: list[dict] = []
    selected = sorted({str(value).strip() for value in system_names if str(value or "").strip()}, key=str.lower)
    for name in selected:
        source_node = definitions.get(name)
        if source_node is None:
            raise ValueError(f"{name}: no EmulationStation system definition is installed locally")
        remote_dir = mount_point / "roms" / name
        _validated_game_paths(remote_dir)
        node = copy.deepcopy(source_node)
        path_node = node.find("path")
        if path_node is None:
            path_node = ET.SubElement(node, "path")
        path_node.text = str(remote_dir)
        root.append(node)
        rows.append({
            "system": name,
            "overlay_created": True,
            "symlink_created": False,
            "had_local_collision": (settings.roms_root / name).exists(),
            "renamed_to": "",
            "skipped_reason": "",
        })
    return ET.ElementTree(root), rows


def _read_settings_tree(settings: Settings) -> ET.ElementTree:
    try:
        return ET.parse(settings.es_settings_file) if settings.es_settings_file.exists() else ET.ElementTree(ET.Element("map"))
    except (OSError, ET.ParseError) as error:
        raise ValueError(f"EmulationStation settings are not readable and valid: {error}") from error


def _capture_parse_gamelist_setting(root: ET.Element) -> dict:
    node = root.find(f".//*[@name='{PARSE_GAMELIST_SETTING}']")
    if node is None:
        return {"present": False, "tag": "bool", "value": "false"}
    return {"present": True, "tag": node.tag, "value": str(node.get("value") or "")}


def _set_parse_gamelist_only(settings: Settings, enabled: bool, previous: Optional[dict] = None) -> dict:
    tree = _read_settings_tree(settings)
    root = tree.getroot()
    captured = previous if isinstance(previous, dict) else _capture_parse_gamelist_setting(root)
    nodes = list(root.findall(f".//*[@name='{PARSE_GAMELIST_SETTING}']"))
    node = nodes[0] if nodes else ET.SubElement(root, "bool")
    node.tag = "bool"
    node.set("name", PARSE_GAMELIST_SETTING)
    node.set("value", "true" if enabled else "false")
    for duplicate in nodes[1:]:
        root.remove(duplicate)
    _atomic_write_xml(settings.es_settings_file, tree)
    return captured


def install(settings: Settings, mount_point: Path, system_names: Iterable[str], previous_setting: Optional[dict] = None) -> tuple[list[dict], Optional[dict]]:
    """Validate remote gamelists, atomically install the overlay, and enable
    gamelist-only discovery. Rolls both files back if the transaction fails."""
    selected = sorted({str(value).strip() for value in system_names if str(value or "").strip()}, key=str.lower)
    if not selected:
        # BIOS-only references do not need to alter EmulationStation at all.
        return [], previous_setting
    target = overlay_path(settings)
    old_overlay: Optional[bytes] = None
    if target.exists():
        try:
            existing_root = ET.parse(target).getroot()
        except (OSError, ET.ParseError) as error:
            raise ValueError(f"Refusing to replace unreadable {target.name}: {error}") from error
        if existing_root.get(OVERLAY_OWNER_ATTRIBUTE) != OVERLAY_OWNER_VALUE:
            raise ValueError(f"Refusing to replace non-Drone EmulationStation overlay {target.name}")
        old_overlay = target.read_bytes()

    tree, rows = _build_overlay(settings, mount_point, selected)
    captured: Optional[dict] = None
    _atomic_write_xml(target, tree)
    try:
        captured = _set_parse_gamelist_only(settings, True, previous_setting)
    except Exception:
        if old_overlay is None:
            target.unlink(missing_ok=True)
        else:
            _atomic_write_bytes(target, old_overlay)
        raise
    return rows, captured


def remove(settings: Settings, previous_setting: Optional[dict]) -> list[str]:
    """Remove only Drone's owned overlay and restore the exact prior setting."""
    errors: list[str] = []
    target = overlay_path(settings)
    if target.exists():
        try:
            root = ET.parse(target).getroot()
            if root.get(OVERLAY_OWNER_ATTRIBUTE) != OVERLAY_OWNER_VALUE:
                errors.append(f"refused to remove non-Drone EmulationStation overlay {target.name}")
            else:
                target.unlink()
        except (OSError, ET.ParseError) as error:
            errors.append(f"could not safely remove {target.name}: {error}")
    if isinstance(previous_setting, dict):
        try:
            tree = _read_settings_tree(settings)
            root = tree.getroot()
            for node in list(root.findall(f".//*[@name='{PARSE_GAMELIST_SETTING}']")):
                root.remove(node)
            if previous_setting.get("present"):
                node = ET.SubElement(root, str(previous_setting.get("tag") or "bool"))
                node.set("name", PARSE_GAMELIST_SETTING)
                node.set("value", str(previous_setting.get("value") or ""))
            _atomic_write_xml(settings.es_settings_file, tree)
        except Exception as error:
            errors.append(f"could not restore {PARSE_GAMELIST_SETTING}: {error}")
    return errors
