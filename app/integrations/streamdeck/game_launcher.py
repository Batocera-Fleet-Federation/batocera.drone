"""GameLauncher: start one library game through Batocera's normal launch path.

Validation confines the ROM to ``<roms root>/<system>/`` (after resolving
symlinks, so network-referenced systems still work) and rejects missing or
escaping paths before anything is launched. The launch itself is delegated to
EmulationStation's local API -- see ``emulationstation.py`` for why that, and
not a direct ``emulatorlauncher``/emulator invocation, is Batocera's normal
path. There is deliberately no shell or direct-emulator fallback.
"""

import os
import re
from pathlib import Path
from typing import Optional

from .config import normalize_rom_path
from .emulationstation import EmulationStationApi, EmulationStationUnavailable


_SYSTEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")


class GameLauncher:
    def __init__(self, roms_root: Path, es_api: Optional[EmulationStationApi] = None) -> None:
        self.roms_root = Path(os.path.abspath(str(roms_root)))
        self.es_api = es_api or EmulationStationApi()

    def resolve_launch_configuration(self, game: dict) -> dict:
        """Where ES knows this game, after confinement checks. Never raises."""
        system = str((game or {}).get("system") or "")
        if not _SYSTEM.fullmatch(system):
            return {"available": False, "reason": "The game's system identifier is invalid."}
        try:
            relative = normalize_rom_path((game or {}).get("rom_path"))
        except ValueError:
            return {"available": False, "reason": "The game's ROM path is invalid. Relink this button."}
        launch_path = self.roms_root / system / relative
        try:
            system_dir = (self.roms_root / system).resolve(strict=True)
            resolved = launch_path.resolve(strict=True)
            resolved.relative_to(system_dir)
        except (OSError, ValueError, RuntimeError):
            return {"available": False, "reason": "Game not found in the local library. Relink this button."}
        if not (resolved.is_file() or resolved.is_dir()):
            return {"available": False, "reason": "Game not found in the local library. Relink this button."}
        return {
            "available": True,
            "reason": "",
            "system": system,
            "rom_path": relative,
            "launch_path": str(launch_path),
            "launcher": "emulationstation-api",
        }

    def validate(self, game: dict) -> dict:
        return self.resolve_launch_configuration(game)

    def get_compatibility(self, game: dict) -> dict:
        resolved = self.resolve_launch_configuration(game)
        if resolved["available"] and not self.es_api.reachable():
            return {"available": False, "reason": "EmulationStation is not running (its local API on port 1234 did not answer)."}
        return {"available": resolved["available"], "reason": resolved["reason"]}

    def launch(self, game: dict) -> dict:
        resolved = self.resolve_launch_configuration(game)
        if not resolved["available"]:
            return {"status": "error", "stage": "validate", "error": resolved["reason"]}
        try:
            status, body = self.es_api.launch(resolved["launch_path"])
        except EmulationStationUnavailable as error:
            return {"status": "error", "stage": "launch",
                    "error": f"EmulationStation is not running or its local API is unavailable ({error})."}
        if status == 200:
            return {"status": "accepted", "launch_path": resolved["launch_path"], "launcher": "emulationstation-api"}
        if status == 404:
            return {"status": "error", "stage": "launch",
                    "error": "EmulationStation does not list this game. Update the gamelists in EmulationStation, then try again."}
        return {"status": "error", "stage": "launch", "error": f"EmulationStation rejected the launch (HTTP {status}): {body}"}
