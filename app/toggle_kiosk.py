#!/usr/bin/env python3
"""Apply Batocera's EmulationStation kiosk setting as the privileged service worker."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


CONFIG = Path("/userdata/system/configs/emulationstation/es_settings.cfg")
EMULATIONSTATION_SERVICE = Path("/etc/init.d/S31emulationstation")


def set_kiosk_mode(enabled: bool) -> None:
    target = "Kiosk" if enabled else "Full"
    if EMULATIONSTATION_SERVICE.exists():
        subprocess.run([str(EMULATIONSTATION_SERVICE), "stop"], check=True)
        time.sleep(2)

    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    try:
        tree = ET.parse(CONFIG) if CONFIG.exists() else ET.ElementTree(ET.Element("map"))
    except (OSError, ET.ParseError):
        tree = ET.ElementTree(ET.Element("map"))
    root = tree.getroot()
    node = root.find(".//string[@name='UIMode']")
    if node is None:
        node = ET.SubElement(root, "string")
        node.set("name", "UIMode")
    node.set("value", target)
    tree.write(CONFIG, encoding="utf-8", xml_declaration=True)

    overlay_tool = "/usr/bin/batocera-save-overlay"
    if not os.path.exists(overlay_tool):
        overlay_tool = "batocera-save-overlay"
    subprocess.run([overlay_tool], check=True)

    if EMULATIONSTATION_SERVICE.exists():
        subprocess.Popen(
            [str(EMULATIONSTATION_SERVICE), "start"],
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
