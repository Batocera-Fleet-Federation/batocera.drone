#!/usr/bin/env python3
"""Apply Batocera's EmulationStation kiosk setting as the privileged service worker.

This is the single, canonical implementation of "flip Kiosk mode and restart
EmulationStation". The privileged (root) Drone service worker invokes it as
``python3 app/toggle_kiosk.py [on|off]`` and the Drone app itself reuses
``set_kiosk_mode`` when it happens to run as root, so the stop -> write -> overlay
-> start sequence below can never drift between the two entry points.

The restart is failure-tolerant on purpose: the ``stop`` and ``batocera-save-overlay``
steps are best-effort, and EmulationStation is ALWAYS restarted afterwards. Running
headless from the Drone service (no TTY), ``batocera-save-overlay`` could fail/hang;
with the old ``check=True`` that aborted before the ``start`` and left a black screen.
Every step is logged to stdout so the service worker captures it for diagnostics.
"""

from __future__ import annotations

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


def _run_step(command: list, *, timeout: int = 120) -> bool:
    """Run a best-effort step, logging its combined output. Never raises."""
    label = " ".join(command)
    try:
        proc = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout
        )
        output = (proc.stdout or "").strip()
        print(f"[toggle_kiosk] {label} -> rc={proc.returncode} {output}".rstrip())
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as error:
        print(f"[toggle_kiosk] {label} -> error: {error}")
        return False


def set_kiosk_mode(enabled: bool, config: Optional[Path] = None) -> None:
    """Flip UIMode and (re)start EmulationStation.

    Stop and overlay-save are best-effort; EmulationStation is always restarted so
    the screen comes back even when an earlier step fails in the headless service
    context. The UIMode is written to es_settings.cfg before the restart, so the new
    mode takes effect as soon as EmulationStation relaunches.
    """
    if config is None:
        config = CONFIG
    target = "Kiosk" if enabled else "Full"
    print(f"[toggle_kiosk] applying UIMode={target}")
    _run_step([EMULATIONSTATION_SERVICE, "stop"], timeout=60)
    time.sleep(2)
    _write_ui_mode(config, target)
    print(f"[toggle_kiosk] wrote UIMode={target} to {config}")
    # Persist to the overlay so the change survives a reboot, but never let a slow or
    # failing overlay save block the EmulationStation restart below.
    _run_step(["batocera-save-overlay"], timeout=120)
    print("[toggle_kiosk] starting EmulationStation")
    subprocess.Popen(
        [EMULATIONSTATION_SERVICE, "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print("[toggle_kiosk] EmulationStation start issued")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1].lower() not in {"on", "off"}:
        print("Usage: toggle_kiosk.py [on|off]", file=sys.stderr)
        return 2
    set_kiosk_mode(sys.argv[1].lower() == "on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
