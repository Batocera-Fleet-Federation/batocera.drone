#!/usr/bin/env python3
"""Apply Batocera's EmulationStation screen mode as the privileged service worker.

This is the single, canonical implementation of "set screen mode and restart
EmulationStation". The privileged (root) Drone service worker invokes it as
``python3 app/set_screen_mode.py [full|kiosk|kid]`` and the Drone app itself reuses
``set_screen_mode`` when it happens to run as root, so the stop -> write -> overlay
-> start sequence below can never drift between the two entry points.

The restart is failure-tolerant on purpose: the ``stop`` and ``batocera-save-overlay``
steps are best-effort, and EmulationStation is ALWAYS restarted afterwards. Running
headless from the Drone service (no TTY), ``batocera-save-overlay`` could fail/hang;
with the old ``check=True`` that aborted before the ``start`` and left a black screen.
Every step is logged to stdout so the service worker captures it for diagnostics.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional


CONFIG = Path("/userdata/system/configs/emulationstation/es_settings.cfg")
EMULATIONSTATION_SERVICE = "/etc/init.d/S31emulationstation"


def _write_ui_mode(config: Path, target: str) -> None:
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        tree = ET.parse(config) if config.exists() else ET.ElementTree(ET.Element("map"))
    except (OSError, ET.ParseError):
        tree = ET.ElementTree(ET.Element("map"))
    root = tree.getroot()
    node = root.find(".//string[@name='UIMode']")
    if node is None:
        node = ET.SubElement(root, "string")
        node.set("name", "UIMode")
    node.set("value", target)
    tree.write(config, encoding="utf-8", xml_declaration=True)


def _read_ui_mode(config: Path) -> Optional[str]:
    try:
        root = ET.parse(config).getroot()
    except (OSError, ET.ParseError):
        return None
    node = root.find(".//string[@name='UIMode']")
    mode = str(node.get("value") if node is not None else "").strip().lower()
    return mode if mode in {"full", "kiosk", "kid"} else None


def _run_step(command: list, *, timeout: int = 120, capture_output: bool = True) -> bool:
    """Run a best-effort step, logging its combined output. Never raises.

    ``capture_output=False`` for "start": its init-script action backgrounds
    ``startx &`` without redirecting its own stdout/stderr, so a backgrounded
    X/EmulationStation process tree keeps the *inherited* pipe write-end open
    long after the init script itself has returned. With ``stdout=PIPE``,
    ``subprocess.run`` blocks reading that pipe until it sees EOF -- which
    never happens while EmulationStation keeps running -- so "start" would
    spuriously time out on every call (even a perfectly successful one) and
    trigger an unnecessary ``batocera-es-swissknife --restart`` fallback that
    then fights the still-fine first attempt for the display. Without a pipe
    to drain, ``subprocess.run`` only waits for the init script's own (fast)
    exit, matching what actually happens.
    """
    label = " ".join(command)
    try:
        if capture_output:
            proc = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout
            )
            output = (proc.stdout or "").strip()
            print(f"[set_screen_mode] {label} -> rc={proc.returncode} {output}".rstrip())
        else:
            proc = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
            print(f"[set_screen_mode] {label} -> rc={proc.returncode}")
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as error:
        print(f"[set_screen_mode] {label} -> error: {error}")
        return False


def set_screen_mode(mode: str, config: Optional[Path] = None) -> bool:
    """Set UIMode and (re)start EmulationStation.

    If the requested mode is already active, return without touching EmulationStation.
    Otherwise, stop and overlay-save are best-effort; EmulationStation is always
    restarted so the screen comes back even when an earlier step fails in the
    headless service context. The UIMode is written to es_settings.cfg before the
    restart, so the new mode takes effect as soon as EmulationStation relaunches.
    """
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"full", "kiosk", "kid"}:
        raise ValueError("Screen mode must be one of: full, kiosk, kid")
    if config is None:
        config = CONFIG
    if _read_ui_mode(config) == normalized_mode:
        print(f"[set_screen_mode] UIMode is already {normalized_mode.title()}; restart skipped")
        return False
    target = normalized_mode.title()
    print(f"[set_screen_mode] applying UIMode={target}")
    _run_step([EMULATIONSTATION_SERVICE, "stop"], timeout=60)
    time.sleep(2)
    _write_ui_mode(config, target)
    print(f"[set_screen_mode] wrote UIMode={target} to {config}")
    # Persist to the overlay so the change survives a reboot, but never let a slow or
    # failing overlay save block the EmulationStation restart below.
    _run_step(["batocera-save-overlay"], timeout=30)
    print("[set_screen_mode] starting EmulationStation")
    started = _run_step([EMULATIONSTATION_SERVICE, "start"], timeout=60, capture_output=False)
    if not started:
        restart_tool = shutil.which("batocera-es-swissknife")
        if restart_tool:
            started = _run_step([restart_tool, "--restart"], timeout=60)
    if not started:
        raise RuntimeError("EmulationStation did not restart after the screen mode change")
    print("[set_screen_mode] EmulationStation start completed")
    return True


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1].lower() not in {"full", "kiosk", "kid"}:
        print("Usage: set_screen_mode.py [full|kiosk|kid]", file=sys.stderr)
        return 2
    set_screen_mode(sys.argv[1].lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
