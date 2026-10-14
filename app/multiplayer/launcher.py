"""Game launch and process tracking for multiplayer, on the existing Batocera path.

Launching goes through EmulationStation's loopback API (the same route Stream
Deck uses) so the system's real ``es_systems`` command, per-game settings -- and
therefore the session-scoped keys written by ``ConfigGuard`` -- all apply.
Active-game detection reuses ``device.game_activity``; exiting a game uses the
shared ``BatoceraControl.exit_game``. This module never runs an emulator
binary itself and takes no shell strings.
"""

from typing import Any, Optional

from . import models as m

try:
    from ..integrations.shared.batocera_control import BatoceraControl
    from ..integrations.streamdeck.emulationstation import EmulationStationApi, EmulationStationUnavailable
    from ..integrations.streamdeck.game_runtime import GameRuntime, same_rom
except ImportError:  # pragma: no cover - flat execution
    from integrations.shared.batocera_control import BatoceraControl  # type: ignore
    from integrations.streamdeck.emulationstation import EmulationStationApi, EmulationStationUnavailable  # type: ignore
    from integrations.streamdeck.game_runtime import GameRuntime, same_rom  # type: ignore


class GameLauncher:
    def __init__(self, es_api: Optional[Any] = None, runtime: Optional[GameRuntime] = None,
                 control: Optional[BatoceraControl] = None) -> None:
        self._es_api = es_api
        self._runtime = runtime
        self._control = control

    @property
    def es_api(self):
        if self._es_api is None:
            self._es_api = EmulationStationApi()
        return self._es_api

    @property
    def runtime(self) -> GameRuntime:
        if self._runtime is None:
            self._runtime = GameRuntime(es_api=self.es_api)
        return self._runtime

    @property
    def control(self) -> BatoceraControl:
        if self._control is None:
            self._control = BatoceraControl()
        return self._control

    def active_game(self) -> Optional[dict]:
        try:
            return self.runtime.get_active_game()
        except Exception:  # noqa: BLE001 - detection is advisory
            return None

    def is_session_game(self, game_path: str) -> bool:
        active = self.active_game()
        return bool(active) and same_rom(active.get("rom_path", ""), str(game_path))

    def launch_game(self, game_path: str) -> dict:
        try:
            status, body = self.es_api.launch(str(game_path))
        except EmulationStationUnavailable as error:
            raise m.MultiplayerError("es_unavailable", f"EmulationStation is not running or its local API is unavailable ({error}).", 409) from error
        if status == 200:
            return {"status": "accepted", "launcher": "emulationstation-api"}
        if status == 404:
            raise m.MultiplayerError("es_unknown_game", "EmulationStation does not list this game. Update the gamelists, then try again.", 409)
        raise m.MultiplayerError("es_rejected", f"EmulationStation rejected the launch (HTTP {status}).", 409)

    def exit_game(self) -> dict:
        return self.control.exit_game()

    def wait_for_exit(self, timeout: float) -> bool:
        return self.runtime.wait_for_exit(timeout)

    def wait_for_start(self, game_path: str, timeout: float) -> Optional[dict]:
        return self.runtime.wait_for_start(str(game_path), timeout)
