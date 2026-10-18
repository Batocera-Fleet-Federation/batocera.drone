"""Control RGS through Batocera's service manager, including boot persistence."""

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_SERVICE = "rgs_download"


def _run(tool, action):
    try:
        result = subprocess.run(
            [tool, action, *(["user"] if action == "list" else [_SERVICE])],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise OSError(f"RGS service {action} failed: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout or "command failed").strip()[-500:]
        raise OSError(f"RGS service {action} failed: {detail}")
    return result.stdout


def _running(settings):
    script = Path(settings.userdata_root) / "system/rgs/download_service/download_service.py"
    proc = Path("/proc")
    if not proc.is_dir():
        raise OSError("RGS process status is unavailable on this system")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            # RGS can launch Python with an absolute script or from its working directory.
            for arg in args[1:]:
                value = os.fsdecode(arg)
                if value == str(script):
                    return True
                if value in {"download_service.py", "./download_service.py"}:
                    if (entry / "cwd").resolve() == script.parent.resolve():
                        return True
        except (OSError, RuntimeError):
            continue  # Processes can exit during the scan.
    return False


def status(settings):
    installed = (Path(settings.userdata_root) / "system/services" / _SERVICE).is_file()
    state = {"installed": installed, "available": False, "enabled": None, "running": None}
    if not installed:
        return state
    tool = shutil.which("batocera-services")
    if not tool or getattr(settings, "use_fake_data", False):
        return {**state, "error": "Batocera service control is unavailable"}
    try:
        rows = [line.split(";", 1) for line in _run(tool, "list").splitlines()]
        enabled = next(parts[1].strip() == "*" for parts in rows if len(parts) == 2 and parts[0] == _SERVICE)
        return {**state, "available": True, "enabled": enabled, "running": _running(settings)}
    except StopIteration:
        return {**state, "error": "RGS is missing from the Batocera service list"}
    except OSError as error:
        return {**state, "error": str(error)}


def set_enabled(settings, enabled):
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    with _LOCK:
        before = status(settings)
        if not before["available"]:
            raise OSError(before.get("error", "RGS download manager is not installed"))
        tool = shutil.which("batocera-services")
        # Use the original launcher so its Proton VPN pre-start guard still applies.
        for action in (("enable", "start") if enabled else ("stop", "disable")):
            _run(tool, action)
        deadline = time.monotonic() + 5
        while True:
            state = status(settings)
            if state["available"] and state["running"] == enabled and state["enabled"] == enabled:
                return state
            if time.monotonic() >= deadline:
                raise OSError(state.get("error", "RGS did not reach the requested state; check its service logs"))
            time.sleep(0.25)
