#!/usr/bin/env python3
"""Apply EmulationStation collection/display settings as the privileged service worker.

Canonical implementation of "write es_settings.cfg values and restart
EmulationStation", mirroring set_screen_mode.py's proven stop -> write ->
overlay-save -> start sequence (same rationale: the ``stop``/overlay-save
steps are best-effort and headless-safe, EmulationStation is ALWAYS restarted
afterwards, and every step is logged to stdout for the service worker to
capture). The privileged (root) Drone service worker invokes it as
``python3 app/set_es_collections.py <path-to-request-json>``; the Drone app
itself reuses ``apply_es_collections`` in-process when it happens to run as
root, so the sequence can never drift between the two entry points.

Every setting here lives in es_settings.cfg, which EmulationStation only
re-reads at its own startup -- a change made externally while ES is already
running is invisible until it restarts, which is why every field here always
restarts ES rather than trying to apply changes live.

Deliberately self-contained (stdlib only, no imports from the rest of the
``app`` package) like set_screen_mode.py / set_volume.py: the caller (Drone
app, which has full package access) computes the low-level field values --
this script just writes them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

CONFIG = Path("/userdata/system/configs/emulationstation/es_settings.cfg")
EMULATIONSTATION_SERVICE = "/etc/init.d/S31emulationstation"


def _set_typed_value(root: ET.Element, tag: str, name: str, value: str) -> None:
    node = root.find(f".//{tag}[@name='{name}']")
    if node is None:
        node = ET.SubElement(root, tag)
        node.set("name", name)
    node.set("value", value)


def _write_updates(config: Path, updates: dict) -> None:
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        tree = ET.parse(config) if config.exists() else ET.ElementTree(ET.Element("map"))
    except (OSError, ET.ParseError):
        tree = ET.ElementTree(ET.Element("map"))
    root = tree.getroot()

    if "music_volume" in updates:
        level = max(0, min(100, int(updates["music_volume"])))
        _set_typed_value(root, "int", "MusicVolume", str(level))
    if "screensaver_time_ms" in updates:
        _set_typed_value(root, "int", "ScreenSaverTime", str(max(0, int(updates["screensaver_time_ms"]))))
    if "hidden_systems" in updates:
        _set_typed_value(root, "string", "HiddenSystems", str(updates["hidden_systems"] or ""))
    if "auto_collections" in updates:
        _set_typed_value(root, "string", "CollectionSystemsAuto", str(updates["auto_collections"] or ""))
    if "custom_collections" in updates:
        _set_typed_value(root, "string", "CollectionSystemsCustom", str(updates["custom_collections"] or ""))
    for system_name, ungroup in (updates.get("ungroup") or {}).items():
        _set_typed_value(root, "bool", f"{system_name}.ungroup", "true" if ungroup else "false")

    tree.write(config, encoding="utf-8", xml_declaration=True)


def _run_step(command: list, *, timeout: int = 120) -> bool:
    """Run a best-effort step, logging its combined output. Never raises."""
    label = " ".join(command)
    try:
        proc = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout
        )
        output = (proc.stdout or "").strip()
        print(f"[set_es_collections] {label} -> rc={proc.returncode} {output}".rstrip())
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as error:
        print(f"[set_es_collections] {label} -> error: {error}")
        return False


def apply_es_collections(updates: dict, config: Optional[Path] = None) -> None:
    """Write the given es_settings.cfg fields and (re)start EmulationStation.

    ``updates`` may contain any of: ``music_volume`` (int 0-100),
    ``screensaver_time_ms`` (int milliseconds, 0 = disabled), ``hidden_systems``
    (semicolon-joined system names), ``auto_collections`` / ``custom_collections``
    (comma-joined collection names), ``ungroup`` (dict of system name -> bool).
    Only the keys present are changed.
    """
    if not isinstance(updates, dict) or not updates:
        raise ValueError("No updates were provided")
    if config is None:
        config = CONFIG
    print(f"[set_es_collections] applying {sorted(updates.keys())}")
    _run_step([EMULATIONSTATION_SERVICE, "stop"], timeout=60)
    time.sleep(2)
    _write_updates(config, updates)
    print(f"[set_es_collections] wrote updates to {config}")
    # Persist to the overlay so the change survives a reboot, but never let a slow or
    # failing overlay save block the EmulationStation restart below.
    _run_step(["batocera-save-overlay"], timeout=30)
    print("[set_es_collections] starting EmulationStation")
    started = _run_step([EMULATIONSTATION_SERVICE, "start"], timeout=60)
    if not started:
        restart_tool = shutil.which("batocera-es-swissknife")
        if restart_tool:
            started = _run_step([restart_tool, "--restart"], timeout=60)
    if not started:
        raise RuntimeError("EmulationStation did not restart after the collections update")
    print("[set_es_collections] EmulationStation start completed")


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: set_es_collections.py <path-to-request-json>", file=sys.stderr)
        return 2
    try:
        updates = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Unable to read request JSON: {error}", file=sys.stderr)
        return 2
    apply_es_collections(updates)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
