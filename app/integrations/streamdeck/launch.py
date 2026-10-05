"""Launch Game orchestration: exit the current game, wait for it, then launch.

One transition at a time, across threads *and* processes (the hardware worker
and the Drone's "Test Game Launch" share ``state/launch.lock``). v1 behavior
for competing requests is documented and predictable: while a transition is in
progress, additional Launch Game requests are rejected with ``status: "busy"``
(no queueing, never a second emulator).

The sequence never sleeps blindly: it reuses the central ``exit-game`` built-in
and then waits on GameRuntime for the emulator process to actually be gone. If
that does not happen within the timeout, the new game is NOT launched.
"""

import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Optional

from .actions import ActionContext, BuiltInActionRegistry
from .game_launcher import GameLauncher
from .game_runtime import GameRuntime, same_rom
from .logs import log_event
from .paths import try_file_lock


IDLE = "IDLE"
EXITING_CURRENT_GAME = "EXITING_CURRENT_GAME"
LAUNCHING_GAME = "LAUNCHING_GAME"
RUNNING = "RUNNING"
ERROR = "ERROR"


class LaunchGameCoordinator:
    def __init__(
        self,
        game_runtime: GameRuntime,
        launcher: GameLauncher,
        actions: BuiltInActionRegistry,
        *,
        lock_path: Optional[Path] = None,
        exit_timeout: float = 20.0,
        start_timeout: float = 45.0,
    ) -> None:
        self.game_runtime = game_runtime
        self.launcher = launcher
        self.actions = actions
        self.lock_path = lock_path
        self.exit_timeout = exit_timeout
        self.start_timeout = start_timeout
        self._lock = threading.Lock()
        self._state = IDLE
        self.last_result: Optional[dict] = None

    @property
    def state(self) -> str:
        return self._state

    def _transition(self, state: str) -> None:
        self._state = state

    def launch(self, game: dict, context: Optional[ActionContext] = None) -> dict:
        context = context or ActionContext(trigger="test")
        summary = {"id": game.get("id"), "name": game.get("name"), "system": game.get("system")}
        if not self._lock.acquire(blocking=False):
            return self._reject(summary, context)
        try:
            file_lock = try_file_lock(self.lock_path) if self.lock_path else nullcontext(True)
            with file_lock as acquired:
                if not acquired:
                    return self._reject(summary, context)
                result = self._run(game, summary, context)
        except Exception as error:  # noqa: BLE001 - always a structured result
            result = {"status": "error", "stage": "internal", "error": str(error), "game": summary}
            self._transition(ERROR)
        finally:
            self._lock.release()
        self.last_result = result
        log_event("game-launch-result", status=result.get("status"), stage=result.get("stage"),
                  game_id=summary["id"], system=summary["system"], error=result.get("error"),
                  duration=result.get("duration_seconds"), trigger=context.trigger)
        return result

    def _reject(self, summary: dict, context: ActionContext) -> dict:
        log_event("game-launch-rejected", reason="launch already in progress", game_id=summary["id"], trigger=context.trigger)
        return {"status": "busy", "error": "A game launch is already in progress.", "game": summary}

    def _run(self, game: dict, summary: dict, context: ActionContext) -> dict:
        started = time.monotonic()

        def done(status: str, **values) -> dict:
            self._transition({"launched": RUNNING, "already-running": RUNNING, "launch-unconfirmed": IDLE}.get(status, ERROR))
            return {"status": status, "game": summary, "duration_seconds": round(time.monotonic() - started, 3), **values}

        log_event("game-launch-requested", game_id=summary["id"], name=summary["name"], system=summary["system"],
                  trigger=context.trigger, device_id=context.device_id, key=context.key_index,
                  requested_by=context.requested_by)
        validation = self.launcher.validate(game)
        if not validation.get("available"):
            return done("error", stage="validate", error=validation.get("reason"))

        previous = self.game_runtime.get_active_game()
        if previous:
            previous_summary = {key: previous.get(key) for key in ("system", "rom_path", "name", "emulator", "pid")}
            if same_rom(previous.get("rom_path", ""), validation["launch_path"]):
                return done("already-running", previous_game=previous_summary,
                            message="That game is already running.")
            self._transition(EXITING_CURRENT_GAME)
            log_event("game-exit-requested", previous_system=previous.get("system"), previous_rom=previous.get("rom_path"))
            exit_context = ActionContext.for_game(previous, trigger=context.trigger, device_id=context.device_id,
                                                  profile_id=context.profile_id, key_index=context.key_index,
                                                  requested_by=context.requested_by, confirmed=True)
            exit_result = self.actions.execute("exit-game", exit_context)
            if exit_result.get("status") != "ok" and self.game_runtime.is_game_running():
                return done("error", stage="exit", previous_game=previous_summary,
                            error=f"Could not exit the current game: {exit_result.get('error') or exit_result.get('status')}")
            if not self.game_runtime.wait_for_exit(self.exit_timeout):
                log_event("game-exit-timeout", timeout=self.exit_timeout, previous_rom=previous.get("rom_path"))
                return done("error", stage="exit-timeout", previous_game=previous_summary,
                            error=f"The current game did not exit within {int(self.exit_timeout)} seconds; "
                                  "the new game was not launched.")
            log_event("game-exit-completed", previous_rom=previous.get("rom_path"))
        else:
            previous_summary = None

        self._transition(LAUNCHING_GAME)
        log_event("game-launch-started", game_id=summary["id"], launch_path=validation["launch_path"])
        launch_result = self.launcher.launch(game)
        if launch_result.get("status") != "accepted":
            return done("error", stage=launch_result.get("stage", "launch"), previous_game=previous_summary,
                        error=launch_result.get("error"))
        observed = self.game_runtime.wait_for_start(validation["launch_path"], self.start_timeout)
        if observed is None:
            return done("launch-unconfirmed", previous_game=previous_summary,
                        message="EmulationStation accepted the launch, but the game was not observed starting yet.")
        return done("launched", previous_game=previous_summary, pid=observed.get("pid"))
