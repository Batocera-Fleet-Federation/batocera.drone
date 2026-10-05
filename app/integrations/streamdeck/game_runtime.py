"""GameRuntime: is a game running, which one, and has it exited?

Reuses the Drone's existing active-game detection
(``device.game_activity.find_running_emulatorlauncher`` -- the same procfs
scan behind idle-game-exit and gameplay history). The process disappearing is
the primary exit signal; EmulationStation's ``/runningGame`` only adds a short,
bounded settle so a new launch never races ES's own post-game bookkeeping.
"""

import shlex
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

try:
    from ...device.game_activity import find_running_emulatorlauncher
except ImportError:  # pragma: no cover - flat execution
    from device.game_activity import find_running_emulatorlauncher  # type: ignore

from .emulationstation import EmulationStationApi


ES_SETTLE_SECONDS = 5.0
GameListener = Callable[[str, dict], None]


def _argument(parts: List[str], flag: str) -> str:
    for index, part in enumerate(parts[:-1]):
        if part == flag:
            return parts[index + 1]
    return ""


def describe_active_game(raw: Optional[dict]) -> Optional[dict]:
    """Normalize ``find_running_emulatorlauncher`` output into the GameRuntime shape."""
    if not raw:
        return None
    try:
        parts = shlex.split(str(raw.get("cmdline") or ""))
    except ValueError:
        parts = str(raw.get("cmdline") or "").split()
    rom_path = str(raw.get("rom_path") or "")
    return {
        "system": str(raw.get("system_name") or _argument(parts, "-system") or ""),
        "rom_path": rom_path,
        "name": Path(rom_path).stem if rom_path else "",
        "emulator": _argument(parts, "-emulator"),
        "core": _argument(parts, "-core"),
        "pid": raw.get("pid"),
    }


def same_rom(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return False


class GameRuntime:
    def __init__(
        self,
        detector: Callable[[], Optional[dict]] = find_running_emulatorlauncher,
        es_api: Optional[EmulationStationApi] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        settle_seconds: float = ES_SETTLE_SECONDS,
    ) -> None:
        self._detector = detector
        self._es_api = es_api
        self._clock = clock
        self._sleep = sleep
        self.settle_seconds = settle_seconds
        self._listeners: List[GameListener] = []
        self._last_seen: Optional[dict] = None
        self._started_at: Optional[str] = None
        self._lock = threading.Lock()

    def get_active_game(self) -> Optional[dict]:
        game = describe_active_game(self._detector())
        if game is not None:
            with self._lock:
                if self._last_seen and same_rom(self._last_seen.get("rom_path", ""), game["rom_path"]):
                    game["started_at"] = self._started_at
        return game

    def is_game_running(self) -> bool:
        return self.get_active_game() is not None

    def wait_for_exit(self, timeout: float, poll_seconds: float = 0.25) -> bool:
        """Block until the emulator process is gone (bounded). Never a blind sleep."""
        deadline = self._clock() + max(0.0, float(timeout))
        while self.is_game_running():
            if self._clock() >= deadline:
                return False
            self._sleep(poll_seconds)
        if self._es_api is not None:
            settle_deadline = min(deadline, self._clock() + self.settle_seconds)
            while self._es_api.running_game() and self._clock() < settle_deadline:
                self._sleep(poll_seconds)
        return True

    def wait_for_start(self, rom_path: str, timeout: float, poll_seconds: float = 0.25) -> Optional[dict]:
        deadline = self._clock() + max(0.0, float(timeout))
        while True:
            game = self.get_active_game()
            if game and same_rom(game.get("rom_path", ""), rom_path):
                return game
            if self._clock() >= deadline:
                return None
            self._sleep(poll_seconds)

    # -- lifecycle subscriptions (used by contextual profile rules) ----------
    def subscribe(self, listener: GameListener) -> None:
        self._listeners.append(listener)

    def subscribe_to_game_start(self, listener: Callable[[dict], None]) -> None:
        self.subscribe(lambda event, game: listener(game) if event == "game-start" else None)

    def subscribe_to_game_stop(self, listener: Callable[[dict], None]) -> None:
        self.subscribe(lambda event, game: listener(game) if event == "game-stop" else None)

    def poll(self) -> List[tuple]:
        """Detect start/stop transitions since the last poll and notify listeners."""
        current = self.get_active_game()
        events = []
        with self._lock:
            previous = self._last_seen
            changed = (previous is None) != (current is None) or (
                previous is not None and current is not None
                and not same_rom(previous.get("rom_path", ""), current.get("rom_path", ""))
            )
            if changed:
                if previous is not None:
                    events.append(("game-stop", previous))
                if current is not None:
                    self._started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                    current["started_at"] = self._started_at
                    events.append(("game-start", current))
                self._last_seen = current
        for event, game in events:
            for listener in list(self._listeners):
                try:
                    listener(event, game)
                except Exception:  # noqa: BLE001 - one bad listener must not stop others
                    pass
        return events
