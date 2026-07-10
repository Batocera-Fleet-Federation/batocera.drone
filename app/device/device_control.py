"""Device-control operations for the Drone (screen mode, audio volume, theme /
EmulationStation config, and privileged actions via the control worker).

Extracted from ``drone_api.py``. The privileged actions (volume, screen mode,
EmulationStation restart, ROM-permission repair) are requested by dropping a
request file in the control directory that the root-side control worker polls;
these helpers write the request and wait for the ack. Theme / ``es_systems.cfg``
resolution parses Batocera + EmulationStation config files. Self-contained:
stdlib + ``Settings`` + the ``set_screen_mode`` / ``set_volume`` helpers.
"""

import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from ..common.settings import Settings
    from ..set_screen_mode import set_screen_mode as _set_screen_mode_helper
    from ..set_volume import set_audio_volume as _set_audio_volume_helper
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from set_screen_mode import set_screen_mode as _set_screen_mode_helper  # type: ignore
    from set_volume import set_audio_volume as _set_audio_volume_helper  # type: ignore


def _parse_batocera_theme_name(conf_path: Path) -> Optional[str]:
    if not conf_path.exists() or not conf_path.is_file():
        return None
    try:
        for raw_line in conf_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in ("global.theme", "system.theme"):
                candidate = value.strip().strip('"').strip("'")
                if candidate:
                    return candidate
    except Exception:
        return None
    return None


def _parse_es_theme_name(es_settings_path: Path) -> Optional[str]:
    if not es_settings_path.exists() or not es_settings_path.is_file():
        return None
    try:
        text = es_settings_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    # EmulationStation usually stores this as:
    # <string name="ThemeSet" value="carbon"/>
    match = re.search(r'<string\s+name="ThemeSet"\s+value="([^"]+)"', text, flags=re.IGNORECASE)
    if not match:
        return None
    theme = match.group(1).strip()
    return theme or None


def _set_screen_mode(settings: Settings, mode: str) -> Path:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"full", "kiosk", "kid"}:
        raise ValueError("Screen mode must be one of: full, kiosk, kid")
    path = settings.es_settings_file
    target = normalized_mode.title()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tree = ET.parse(path) if path.exists() else ET.ElementTree(ET.Element("map"))
    except ET.ParseError:
        tree = ET.ElementTree(ET.Element("map"))
    root = tree.getroot()
    node = root.find(".//string[@name='UIMode']")
    if node is None:
        node = ET.SubElement(root, "string")
        node.set("name", "UIMode")
    node.set("value", target)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def _get_screen_mode(settings: Settings) -> Optional[str]:
    try:
        root = ET.parse(settings.es_settings_file).getroot()
    except Exception:
        return None
    node = root.find(".//string[@name='UIMode']")
    if node is None:
        return None
    mode = str(node.get("value") or "").strip().lower()
    return mode if mode in {"full", "kiosk", "kid"} else None


def _get_audio_volume(settings: Settings) -> Optional[int]:
    """Best-effort read of the current output volume (0-100). Read-only, no root."""
    getter = shutil.which("batocera-settings-get")
    if getter:
        try:
            result = subprocess.run(
                [getter, "audio.volume"], capture_output=True, text=True, timeout=5
            )
            value = (result.stdout or "").strip()
            if value.isdigit():
                return max(0, min(100, int(value)))
        except (OSError, subprocess.SubprocessError):
            pass
    audio = shutil.which("batocera-audio")
    if audio:
        try:
            result = subprocess.run(
                [audio, "getSystemVolume"], capture_output=True, text=True, timeout=5
            )
            match = re.search(r"\d{1,3}", result.stdout or "")
            if match:
                return max(0, min(100, int(match.group())))
        except (OSError, subprocess.SubprocessError):
            pass
    amixer = shutil.which("amixer")
    if amixer:
        try:
            result = subprocess.run(
                [amixer, "sget", "Master"], capture_output=True, text=True, timeout=5
            )
            match = re.search(r"\[(\d{1,3})%\]", result.stdout or "")
            if match:
                return max(0, min(100, int(match.group(1))))
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _request_service_control(command: str, body: Optional[str] = None) -> bool:
    if command not in {
        "restart-emulationstation",
        "set-screen-mode-full",
        "set-screen-mode-kiosk",
        "set-screen-mode-kid",
        "repair-rom-permissions",
        "kill-emulator",
    }:
        return False
    control_dir = Path(os.environ.get("DRONE_SERVICE_CONTROL_DIR", "/userdata/system/drone-app/control"))
    request_path = control_dir / f"{command}.request"
    try:
        control_dir.mkdir(parents=True, exist_ok=True)
        # The privileged worker reads the first line: for parametrized commands
        # that is the argument (e.g. a system name); otherwise a timestamp marker.
        content = body if body is not None else datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        request_path.write_text(content + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def _request_rom_permission_repair(system: str = "") -> bool:
    """Best-effort: ask the privileged service worker to make a ROM system's
    images/ dir and gamelist.xml group-writable so the unprivileged Drone can
    place artwork and update the gamelist. No-op outside the on-device service."""
    try:
        return _request_service_control("repair-rom-permissions", body=str(system or "").strip())
    except Exception:
        return False


def _ensure_rom_write_access(settings: Settings, system: str = "", timeout_seconds: float = 5.0) -> bool:
    """Synchronously ask the privileged service worker to make a ROM system's media
    dirs + gamelist.xml writable by the Drone, waiting briefly for confirmation so a
    following write actually succeeds. Best-effort: returns True only on a confirmed
    "ok"; returns False on failure/timeout and never raises (e.g. off-device, or when
    the privileged worker is unavailable)."""
    control_dir = Path(os.environ.get("DRONE_SERVICE_CONTROL_DIR", "/userdata/system/drone-app/control"))
    result_path = control_dir / "repair-rom-permissions.result"
    try:
        result_path.unlink(missing_ok=True)
    except OSError:
        pass
    if not _request_rom_permission_repair(system):
        return False
    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    while time.monotonic() < deadline:
        try:
            if result_path.exists():
                result = result_path.read_text(encoding="utf-8", errors="ignore").strip()
                result_path.unlink(missing_ok=True)
                return result == "ok"
        except OSError:
            return False
        time.sleep(0.2)
    return False


def _request_screen_mode_service_control(mode: str) -> bool:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"full", "kiosk", "kid"}:
        raise ValueError("Screen mode must be one of: full, kiosk, kid")
    command = f"set-screen-mode-{normalized_mode}"
    control_dir = Path(os.environ.get("DRONE_SERVICE_CONTROL_DIR", "/userdata/system/drone-app/control"))
    request_path = control_dir / f"{command}.request"
    result_path = control_dir / f"{command}.result"
    try:
        result_path.unlink(missing_ok=True)
    except OSError:
        pass
    if not _request_service_control(command):
        return False
    deadline = time.monotonic() + max(3.0, float(os.environ.get("DRONE_SERVICE_CONTROL_TIMEOUT_SECONDS", "120")))
    while time.monotonic() < deadline:
        try:
            if result_path.exists():
                result = result_path.read_text(encoding="utf-8", errors="ignore").strip()
                result_path.unlink(missing_ok=True)
                if result == "ok":
                    return True
                raise OSError(result or "Privileged screen mode operation failed")
        except OSError:
            raise
        time.sleep(0.25)
    try:
        request_path.unlink(missing_ok=True)
    except OSError:
        pass
    raise OSError("Timed out waiting for the privileged screen mode service operation")


def _request_volume_service_control(level: int) -> bool:
    level = max(0, min(100, int(level)))
    control_dir = Path(os.environ.get("DRONE_SERVICE_CONTROL_DIR", "/userdata/system/drone-app/control"))
    request_path = control_dir / "set-volume.request"
    result_path = control_dir / "set-volume.result"
    try:
        result_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        control_dir.mkdir(parents=True, exist_ok=True)
        request_path.write_text(f"{level}\n", encoding="utf-8")
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
                raise OSError(result or "Privileged volume operation failed")
        except OSError:
            raise
        time.sleep(0.25)
    try:
        request_path.unlink(missing_ok=True)
    except OSError:
        pass
    raise OSError("Timed out waiting for the privileged volume service operation")


def _apply_audio_volume(settings: Settings, level: int) -> int:
    """Apply the output volume (0-100), returning the clamped level that was set."""
    level = max(0, min(100, int(level)))
    if settings.use_fake_data:
        return level
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        _set_audio_volume_helper(level)
        return level
    if not _request_volume_service_control(level):
        raise OSError(
            "Unable to dispatch the privileged volume request; the Drone service "
            "control worker may not be running."
        )
    return level


# --- Device automation (idle-volume) -------------------------------------------------
#
# The Drone runs unprivileged and cannot read the kernel input devices, so the root
# service control worker runs ``input_activity_monitor.py``, which records the wall-clock
# epoch of the most recent controller/keyboard/mouse event in a small file. The poller
# below reads that file and lowers the volume once the device has been idle long enough.


def _emulationstation_restart_command() -> Optional[List[str]]:
    init_script = Path("/etc/init.d/S31emulationstation")
    if init_script.exists():
        return [str(init_script), "restart"]
    restart_tool = shutil.which("batocera-es-swissknife")
    if restart_tool:
        return [restart_tool, "--restart"]
    return None


def _restart_emulationstation() -> bool:
    if _request_service_control("restart-emulationstation"):
        return True
    command = _emulationstation_restart_command()
    if not command:
        return False
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def _emulator_kill_command() -> Optional[List[str]]:
    kill_tool = shutil.which("batocera-es-swissknife")
    if kill_tool:
        return [kill_tool, "--emukill"]
    return None


def _kill_running_emulator() -> bool:
    """Exit the currently running game, returning to EmulationStation."""
    if _request_service_control("kill-emulator"):
        return True
    command = _emulator_kill_command()
    if not command:
        return False
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def _apply_screen_mode(settings: Settings, mode: str) -> tuple[Path, bool]:
    if settings.use_fake_data:
        return _set_screen_mode(settings, mode), False
    # When the Drone app already runs as root, apply the change directly using the
    # proven stop -> write -> overlay -> start sequence shared with set_screen_mode.py.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        _set_screen_mode_helper(mode, config=settings.es_settings_file)
        return settings.es_settings_file, True
    # Non-root (production): the privileged service worker runs set_screen_mode.py for us.
    # _request_screen_mode_service_control returns True only after the worker reports "ok"
    # and raises on failure/timeout, so the result we report back to Overmind reflects
    # what actually happened instead of falsely claiming an EmulationStation restart.
    if not _request_screen_mode_service_control(mode):
        raise OSError(
            "Unable to dispatch the privileged screen mode request; the Drone service "
            "control worker may not be running."
        )
    return settings.es_settings_file, True


def _resolve_es_settings_file(settings: Settings) -> Optional[Path]:
    candidates = [
        settings.es_settings_file,
        Path("/userdata/system/configs/emulationstation/es_settings.cfg"),
        Path("/userdata/system/.emulationstation/es_settings.cfg"),
    ]
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return candidate
        except Exception:
            continue
    return None


def _parse_es_systems_cfg(path: Path) -> List[dict]:
    if not path.exists() or not path.is_file():
        return []
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception:
        return []

    systems: List[dict] = []
    for system_node in root.findall(".//system"):
        data: dict = {}
        for tag in ("name", "fullname", "path", "extension", "command", "platform", "theme"):
            node = system_node.find(tag)
            if node is not None and node.text is not None:
                data[tag] = node.text.strip()
        hidden_attr = system_node.attrib.get("hidden")
        hidden_node = system_node.find("hidden")
        hidden_text = hidden_node.text.strip() if hidden_node is not None and hidden_node.text else ""
        hidden_value = (hidden_attr or hidden_text or "").strip().lower()
        data["hidden"] = hidden_value in ("1", "true", "yes", "on")
        if data.get("name"):
            systems.append(data)
    return systems


def _resolve_es_systems_effective(settings: Settings) -> Tuple[Optional[Path], List[dict]]:
    userdata_root = settings.userdata_root.resolve()
    override = (userdata_root / "system" / "configs" / "emulationstation" / "es_systems.cfg").resolve()
    base = settings.es_systems_file

    source_path = override if override.exists() and override.is_file() else base
    systems = _parse_es_systems_cfg(source_path)

    # Apply overlays (es_systems_<name>.cfg) by replacing/adding per <name>.
    overlay_dir = (userdata_root / "system" / "configs" / "emulationstation").resolve()
    overlays: List[Path] = []
    if overlay_dir.exists() and overlay_dir.is_dir():
        overlays = sorted(
            [p for p in overlay_dir.glob("es_systems_*.cfg") if p.is_file()],
            key=lambda p: p.name.lower(),
        )

    by_name = {item.get("name"): item for item in systems if item.get("name")}
    for overlay in overlays:
        for item in _parse_es_systems_cfg(overlay):
            name = item.get("name")
            if not name:
                continue
            by_name[name] = item

    merged = list(by_name.values())
    merged.sort(key=lambda item: str(item.get("name", "")).lower())
    return source_path if source_path.exists() else None, merged


def _resolve_theme_dir(settings: Settings) -> Optional[Path]:
    es_settings_file = _resolve_es_settings_file(settings)
    from_es_settings = _parse_es_theme_name(es_settings_file) if es_settings_file else None
    theme_name = (
        settings.batocera_theme_name
        or _parse_batocera_theme_name(settings.batocera_conf_file)
        or from_es_settings
    )
    if theme_name:
        theme_dir = (settings.themes_root / theme_name).resolve()
        if theme_dir.exists() and theme_dir.is_dir():
            return theme_dir

    # Batocera installs can omit explicit theme settings.
    # If exactly one theme directory exists, use it automatically.
    try:
        candidates = sorted(
            [entry.resolve() for entry in settings.themes_root.iterdir() if entry.is_dir()],
            key=lambda p: p.name.lower(),
        )
    except Exception:
        return None

    if len(candidates) == 1:
        return candidates[0]
    return None
