"""Turn on RetroArch's localhost command port so the Save/Load/Pause buttons can reach it.

RetroArch ships with ``network_cmd_enable`` off, and Batocera never switches it on, so
the UDP command interface that ``RetroArchControl`` talks to does not exist on a stock
device. Enabling it means RetroArch listens on UDP port 55355 for every interface unless
``network_cmd_bind_address`` is set, so we always pin the bind address to 127.0.0.1.

The Drone only edits the keys it needs in ``retroarchcustom.cfg`` (the file the running
RetroArch was launched with), records each prior value in the integration's state
directory, and puts those values back when the integration is disabled or removed.
Every other line is left exactly as it was.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .logs import log_event
from .paths import StreamDeckPaths, atomic_write_bytes, atomic_write_json, read_json

REQUIRED_SETTINGS: Tuple[Tuple[str, str], ...] = (
    ("network_cmd_enable", "true"),
    ("network_cmd_bind_address", "127.0.0.1"),
)
STATE_FILE = "retroarch-network.json"
RELATIVE_CONFIG = Path("system/configs/retroarch/retroarchcustom.cfg")


def _parse_value(text: str) -> str:
    return text.strip().strip('"')


class RetroArchNetworkConfig:
    def __init__(self, paths: StreamDeckPaths, userdata_root: Path) -> None:
        self.paths = paths
        self.config_path = Path(userdata_root) / RELATIVE_CONFIG

    @property
    def state_path(self) -> Path:
        return self.paths.owned("state", STATE_FILE)

    def _read_lines(self) -> List[str]:
        return self.config_path.read_text(encoding="utf-8", errors="replace").splitlines()

    @staticmethod
    def _find(lines: List[str], key: str) -> Optional[str]:
        """Return the value of the last ``key = value`` line, or None when absent."""
        found: Optional[str] = None
        for line in lines:
            name, separator, value = line.partition("=")
            if separator and name.strip() == key:
                found = _parse_value(value)
        return found

    @staticmethod
    def _replace(lines: List[str], key: str, value: Optional[str]) -> List[str]:
        """Set ``key`` to ``value`` (or remove it when ``value`` is None), keeping every other line."""
        kept = []
        for line in lines:
            name, separator, _ = line.partition("=")
            if not (separator and name.strip() == key):
                kept.append(line)
        if value is None:
            return kept
        return kept + [f'{key} = "{value}"']

    def ensure(self) -> Dict[str, Any]:
        """Set the required keys if they differ; remember the prior values the first time."""
        if not self.config_path.is_file():
            return {"status": "pending", "message": "RetroArch has not created its config yet; start a game once."}
        lines = self._read_lines()
        state = read_json(self.state_path, default={}) or {}
        changed: Dict[str, Optional[str]] = dict(state.get("changed") or {})
        updated = list(lines)
        for key, value in REQUIRED_SETTINGS:
            current = self._find(updated, key)
            if current is not None and current.lower() == value:
                continue
            changed.setdefault(key, current)
            updated = self._replace(updated, key, value)
        if updated == lines:
            return {"status": "ok", "changed": []}
        self._write(updated)
        atomic_write_json(self.state_path, {"config": str(self.config_path), "changed": changed})
        log_event("retroarch-network-enabled", config=str(self.config_path), keys=sorted(changed))
        return {"status": "changed", "changed": sorted(changed),
                "message": "RetroArch's command interface is enabled. Relaunch a running game for it to take effect."}

    def restore(self) -> Dict[str, Any]:
        """Undo only the changes ``ensure`` made, then forget them."""
        state = read_json(self.state_path, default=None)
        if not state:
            return {"status": "ok", "restored": []}
        restored: List[str] = []
        if self.config_path.is_file():
            lines = self._read_lines()
            for key, prior in (state.get("changed") or {}).items():
                lines = self._replace(lines, key, prior)
                restored.append(key)
            if restored:
                self._write(lines)
        if self.state_path.exists():
            self.paths.remove_owned_tree(self.state_path)
        log_event("retroarch-network-restored", config=str(self.config_path), keys=restored)
        return {"status": "restored", "restored": restored}

    def _write(self, lines: List[str]) -> None:
        mode = self.config_path.stat().st_mode & 0o777
        data = ("\n".join(lines) + "\n").encode("utf-8")
        atomic_write_bytes(self.config_path, data, mode=mode)
