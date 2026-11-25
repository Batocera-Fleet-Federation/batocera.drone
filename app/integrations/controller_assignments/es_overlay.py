"""The one file Controller Assignments writes outside its own directory.

``es_systems_zzz_drone_controllers.cfg`` holds a *partial* ``<system>`` node
(``<name>`` + ``<command>`` only) for every system that has a launch command.
Batocera's EmulationStation merges overlay children into the existing system
definition, so path, extensions, emulators and theme keep coming from
Batocera, RGS, or the Drone's network overlay; only the command gains the
wrapper prefix::

    <wrapper> --runtime <runtime.json> -- <the command that was in effect>

The file sorts after every other overlay (``zzz``) so its command is the
final writer, carries an ownership marker, and is only ever replaced or
removed when that marker is present. EmulationStation reads overlays at
startup, so writing or removing it needs one EmulationStation restart; rule
edits afterwards do not (the wrapper reads ``runtime.json`` per launch).
"""

import os
import re
import shlex
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from ..shared.files import atomic_write_bytes, atomic_write_json, read_json
except ImportError:  # pragma: no cover - flat execution
    from integrations.shared.files import atomic_write_bytes, atomic_write_json, read_json  # type: ignore


OVERLAY_FILENAME = "es_systems_zzz_drone_controllers.cfg"
OWNER_ATTRIBUTE = "data-drone-owned"
OWNER_VALUE = "controller-assignments"
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/+-]+$")


def config_dir(settings) -> Path:
    return Path(settings.userdata_root) / "system" / "configs" / "emulationstation"


def overlay_path(settings) -> Path:
    return config_dir(settings) / OVERLAY_FILENAME


def wrapper_prefix(wrapper: Path, runtime: Path) -> str:
    for value in (str(wrapper), str(runtime)):
        if not _SAFE_PATH.fullmatch(value):
            raise ValueError(f"path is not safe to place in an EmulationStation command: {value}")
    return f"{wrapper} --runtime {runtime} --"


def strip_wrapper(command: str) -> str:
    """Undo our own prefix (defensive: never wrap the wrapper)."""
    text = str(command or "").strip()
    try:
        tokens = shlex.split(text)
    except ValueError:
        return text
    if len(tokens) >= 4 and tokens[0].endswith("/drone-controller-order") and tokens[1] == "--runtime" \
            and tokens[3] == "--":
        marker = f" {tokens[2]} --"
        index = text.find(marker)
        if index >= 0:
            return text[index + len(marker):].strip()
    return text


def _commands_in(path: Path) -> Dict[str, str]:
    try:
        root = ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError):
        return {}
    commands: Dict[str, str] = {}
    for node in root.iter("system"):
        name = str(node.findtext("name") or "").strip()
        command = node.findtext("command")
        if name and command is not None and command.strip():
            commands[name] = command.strip()
    return commands


def _sources(settings) -> List[Path]:
    directory = config_dir(settings)
    override = directory / "es_systems.cfg"
    base = override if override.is_file() else Path(settings.es_systems_file)
    try:
        overlays = sorted((p for p in directory.glob("es_systems_*.cfg") if p.is_file()),
                          key=lambda item: item.name.lower())
    except OSError:
        overlays = []
    return [base] + [p for p in overlays if p.name != OVERLAY_FILENAME]


_CACHE: Dict[str, Tuple[tuple, Dict[str, str]]] = {}


def effective_commands(settings) -> Dict[str, str]:
    """``{system: command}`` as EmulationStation would merge it, minus our overlay."""
    sources = _sources(settings)
    signature = []
    for path in sources:
        try:
            info = path.stat()
            signature.append((str(path), info.st_mtime_ns, info.st_size))
        except OSError:
            signature.append((str(path), 0, 0))
    key = str(config_dir(settings))
    cached = _CACHE.get(key)
    if cached and cached[0] == tuple(signature):
        return dict(cached[1])
    commands: Dict[str, str] = {}
    for path in sources:
        commands.update(_commands_in(path))
    commands = {name: strip_wrapper(command) for name, command in commands.items()}
    _CACHE[key] = (tuple(signature), commands)
    return dict(commands)


_SHELL_SYNTAX = re.compile(r"[;&|`$<>(){}\n]")


def wrappable(command: str) -> bool:
    """Only plain ``program args...`` commands that take player arguments.

    Anything with shell syntax or a leading ``VAR=value`` is left exactly as
    Batocera wrote it, as is a command without ``%CONTROLLERSCONFIG%`` (it
    has no players to reorder).
    """
    if "%CONTROLLERSCONFIG%" not in command or _SHELL_SYNTAX.search(command):
        return False
    try:
        first = shlex.split(command)[0]
    except (ValueError, IndexError):
        return False
    return "=" not in first


def render(settings, wrapper: Path, runtime: Path) -> bytes:
    prefix = wrapper_prefix(wrapper, runtime)
    root = ET.Element("systemList", {OWNER_ATTRIBUTE: OWNER_VALUE})
    commands = {name: command for name, command in effective_commands(settings).items() if wrappable(command)}
    for name, command in sorted(commands.items(), key=lambda item: item[0].lower()):
        node = ET.SubElement(root, "system")
        ET.SubElement(node, "name").text = name
        ET.SubElement(node, "command").text = f"{prefix} {command}"
    ET.indent(root)
    comment = ("<!-- Generated by the Batocera Drone Controller Assignments integration. "
               "Disable the integration to remove it; do not edit. -->\n")
    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + comment + body + "\n").encode("utf-8")


def is_ours(settings) -> bool:
    path = overlay_path(settings)
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return ET.parse(str(path)).getroot().get(OWNER_ATTRIBUTE) == OWNER_VALUE
    except (OSError, ET.ParseError):
        return False


def current(settings) -> Optional[bytes]:
    try:
        return overlay_path(settings).read_bytes() if is_ours(settings) else None
    except OSError:
        return None


def wrapped_systems(settings) -> int:
    data = current(settings)
    if data is None:
        return 0
    try:
        return len(ET.fromstring(data).findall("system"))
    except ET.ParseError:
        return 0


def apply(settings, wrapper: Path, runtime: Path, marker_file: Path) -> dict:
    """Write the overlay if it differs. Returns ``{"changed", "systems"}``."""
    path = overlay_path(settings)
    if path.is_symlink() or (path.exists() and not is_ours(settings)):
        raise ValueError(f"{path} exists and is not managed by the Drone; refusing to overwrite it")
    if config_dir(settings).is_symlink():
        raise ValueError("the EmulationStation configuration directory must not be a symbolic link")
    expected = render(settings, wrapper, runtime)
    changed = current(settings) != expected
    if changed:
        atomic_write_bytes(path, expected, mode=0o644)
        atomic_write_json(marker_file, {"changed_at": time.time()}, mode=0o644)
    return {"changed": changed, "systems": len(ET.fromstring(expected).findall("system"))}


def remove(settings, marker_file: Path) -> dict:
    removed = False
    if is_ours(settings):
        overlay_path(settings).unlink()
        removed = True
        try:
            atomic_write_json(marker_file, {"changed_at": time.time()}, mode=0o644)
        except OSError:
            pass
    return {"removed": removed}


def in_sync(settings, wrapper: Path, runtime: Path) -> bool:
    try:
        return current(settings) == render(settings, wrapper, runtime)
    except (OSError, ValueError):
        return False


def emulationstation_started_at(proc_root: Path = Path("/proc")) -> Optional[float]:
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return None
    started: Optional[float] = None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text(encoding="utf-8").strip() not in ("emulationstatio", "emulationstation"):
                continue
            stamp = os.stat(str(entry)).st_ctime
        except OSError:
            continue
        started = stamp if started is None else min(started, stamp)
    return started


def restart_needed(marker_file: Path, proc_root: Path = Path("/proc")) -> bool:
    """True when the overlay changed after the running EmulationStation started."""
    changed_at = (read_json(marker_file, {}) or {}).get("changed_at")
    started = emulationstation_started_at(proc_root)
    if not isinstance(changed_at, (int, float)) or started is None:
        return False
    return started < changed_at
