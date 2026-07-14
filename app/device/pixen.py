"""PixN script detection and manual trigger helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


PIXEN_UPGRADE_SCRIPT = "/userdata/roms/rgs/rgs_upgrade.sh"


def pixen_script_path(settings: Any) -> Path:
    """Return the PixN upgrade script path, mapped through USERDATA_ROOT in tests."""
    configured = os.environ.get("DRONE_PIXEN_UPGRADE_SCRIPT", PIXEN_UPGRADE_SCRIPT)
    path = Path(configured)
    if str(path) == "/userdata" or str(path).startswith("/userdata/"):
        relative = str(path)[len("/userdata/") :] if str(path) != "/userdata" else ""
        return (Path(settings.userdata_root) / relative).resolve()
    return path.resolve()


def is_pixen_installed(settings: Any) -> bool:
    path = pixen_script_path(settings)
    return path.exists() and path.is_file()


def run_pixen_upgrade(settings: Any) -> dict:
    """Start the PixN upgrade script and return lightweight launch metadata."""
    path = pixen_script_path(settings)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(str(path))
    if getattr(settings, "use_fake_data", False):
        return {
            "type": "pixen_update",
            "status": "started",
            "simulated": True,
            "script": str(path),
        }
    command = [str(path)] if os.access(path, os.X_OK) else ["sh", str(path)]
    process = subprocess.Popen(
        command,
        cwd=str(path.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {
        "type": "pixen_update",
        "status": "started",
        "pid": process.pid,
        "script": str(path),
    }
