#!/usr/bin/env python3
"""Apply Batocera's EmulationStation kiosk setting as the privileged service worker.

This is the single, canonical implementation of "flip Kiosk mode and restart
EmulationStation". The privileged (root) Drone service worker invokes it as
``python3 app/toggle_kiosk.py [on|off]`` and the Drone app itself reuses
``set_kiosk_mode`` when it happens to run as root, so the proven stop -> write ->
overlay -> start sequence below can never drift between the two entry points.
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


def set_kiosk_mode(enabled: bool, config: Optional[Path] = None) -> None:
    """Flip UIMode and restart EmulationStation using the proven sequence.

    Mirrors the known-good standalone script: stop ES, persist the setting and
    overlay it to disk, then relaunch ES detached so it survives this process.
    Raises ``subprocess.CalledProcessError``/``OSError`` on failure so callers
    can report an honest result instead of a black screen.
    """
    if config is None:
        config = CONFIG
    target = "Kiosk" if enabled else "Full"
    subprocess.run([EMULATIONSTATION_SERVICE, "stop"], check=True)
    time.sleep(2)
    _write_ui_mode(config, target)
    subprocess.run(["batocera-save-overlay"], check=True)
    subprocess.Popen(
        [EMULATIONSTATION_SERVICE, "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1].lower() not in {"on", "off"}:
        print("Usage: toggle_kiosk.py [on|off]", file=sys.stderr)
        return 2
    set_kiosk_mode(sys.argv[1].lower() == "on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
