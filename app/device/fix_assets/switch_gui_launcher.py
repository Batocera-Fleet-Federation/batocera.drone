#!/usr/bin/env python3
"""Dynamic Eden/Citron launcher used by Drone's Switch GUI workaround."""

import json
import os
import sys
from pathlib import Path


USERDATA = Path("/userdata")
CONFIG = USERDATA / "system" / "switch-gui-workaround" / "config.json"
EMU_ROOT = USERDATA / "system" / "rgs" / "emulators" / "switch"
WRAPPER = USERDATA / "system" / "rgs" / "generators" / "yuzu" / "drone-switch-gui-autoload.sh"
SWITCH_ROOT = USERDATA / "roms" / "switch"


def load_config():
    try:
        value = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def relative_game_id(rom):
    try:
        return Path(rom).resolve().relative_to(SWITCH_ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return ""


def main(argv):
    supported = {"eden", "eden-legacy", "eden-pgo", "citron", "citron-legacy"}
    if len(argv) != 2 or argv[0] not in supported:
        print("usage: switch_gui_launcher.py <eden|eden-legacy|eden-pgo|citron|citron-legacy> <rom>", file=sys.stderr)
        return 2
    emulator, rom = argv
    config = load_config()
    game_id = relative_game_id(rom)
    selected = {str(value) for value in config.get("selected_games", [])}
    use_gui = bool(config.get("enabled")) and (
        config.get("scope") == "all" or (game_id and game_id in selected)
    )
    appimage = EMU_ROOT / f"{emulator}.AppImage"
    if use_gui and WRAPPER.is_file():
        names = config.get("game_names") if isinstance(config.get("game_names"), dict) else {}
        title = str(names.get(game_id) or Path(rom).stem)
        os.execv("/bin/bash", ["/bin/bash", str(WRAPPER), emulator, rom, title])
    os.execv(str(appimage), [str(appimage), "-f", "-g", rom])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
