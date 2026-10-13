"""Trusted Batocera commands behind the built-in actions (exit, reboot, volume...)."""

import shutil
from typing import Any, Callable, List, Optional

from .process import ProcessRunner


# Stable built-in IDs shared by integrations that bind a physical control to a
# trusted Batocera command (Media Keys). id -> (label, BatoceraControl call,
# needs a deliberate confirmation such as a hold). Never a stored command.
BUILTIN_COMMANDS = {
    "exit-game": ("Exit Current Game", "exit_game", False),
    "reboot": ("Reboot Batocera", "reboot", True),
    "shutdown": ("Shut Down Batocera", "shutdown", True),
    "restart-frontend": ("Restart EmulationStation", "restart_frontend", False),
    "volume-up": ("Volume +5", ("volume", "+5"), False),
    "volume-down": ("Volume -5", ("volume", "-5"), False),
    "volume-mute": ("Toggle Mute", ("volume", "mute-toggle"), False),
}


class BatoceraControl:
    """The only place trusted Batocera commands for built-ins are spelled out.

    Shared by Stream Deck buttons and Media Keys bindings; ``log`` receives
    ``builtin-command`` events so each integration logs under its own tag.
    """

    SWISSKNIFE = "batocera-es-swissknife"
    AUDIO = "batocera-audio"

    def __init__(self, runner: Optional[ProcessRunner] = None,
                 which: Callable[[str], Optional[str]] = shutil.which,
                 restart_frontend: Optional[Callable[[], bool]] = None,
                 log: Optional[Callable[..., Any]] = None) -> None:
        self.runner = runner or ProcessRunner()
        self.which = which
        self._restart_frontend = restart_frontend
        self._log = log or (lambda *_args, **_kwargs: None)

    def _run(self, candidates: List[tuple], *, timeout: float = 30.0, success_codes: tuple = (0,)) -> dict:
        for executable, arguments in candidates:
            path = self.which(executable)
            if not path:
                continue
            result = self.runner.run(path, arguments, timeout=timeout)
            self._log("builtin-command", executable=executable, exit_code=result.exit_code,
                      duration=result.duration_seconds, timed_out=result.timed_out or None)
            if result.exit_code in success_codes and not (result.timed_out or result.cancelled or result.start_error):
                return {"status": "ok", "exit_code": result.exit_code,
                        "duration_seconds": round(result.duration_seconds, 3)}
            return {"status": "error", "error": f"{executable} failed: {result.summary()}"}
        return {"status": "unavailable", "error": f"Required Batocera tool not found: {candidates[0][0]}"}

    def has(self, *executables: str) -> bool:
        return any(self.which(name) for name in executables)

    def exit_game(self) -> dict:
        # Physically verified on Batocera: returns a running game to ES.
        # Newer swissknife returns informational codes: 20 graceful hotkey,
        # 21 already stopped, 22 forced exit, 25 Wine exit requested. The
        # coordinator still waits for actual process termination in all cases.
        return self._run([(self.SWISSKNIFE, ["--emukill"])], success_codes=(0, 20, 21, 22, 25))

    def reboot(self) -> dict:
        return self._run([(self.SWISSKNIFE, ["--reboot"]), ("reboot", [])], timeout=60, success_codes=(0, 10, 11))

    def shutdown(self) -> dict:
        return self._run([(self.SWISSKNIFE, ["--shutdown"]), ("poweroff", [])], timeout=60, success_codes=(0, 10, 11))

    def restart_frontend(self) -> dict:
        if self._restart_frontend is not None:
            ok = self._restart_frontend()
        else:
            # The same mechanism as the admin "Restart EmulationStation" button.
            try:
                from ...device.device_control import _restart_emulationstation
            except ImportError:  # pragma: no cover - flat execution
                from device.device_control import _restart_emulationstation  # type: ignore
            ok = _restart_emulationstation()
        return {"status": "ok"} if ok else {"status": "error", "error": "EmulationStation did not restart."}

    def volume(self, change: str) -> dict:
        amixer = {"+5": "5%+", "-5": "5%-", "mute-toggle": "toggle"}[change]
        return self._run([(self.AUDIO, ["setSystemVolume", change]), ("amixer", ["-q", "sset", "Master", amixer])])

    def run_builtin(self, action_id: str) -> dict:
        """Run one catalog entry by ID; unknown IDs are refused, never executed."""
        entry = BUILTIN_COMMANDS.get(action_id)
        if entry is None:
            return {"status": "error", "error": f"Unknown built-in action: {action_id}"}
        call = entry[1]
        if isinstance(call, tuple):
            return getattr(self, call[0])(call[1])
        return getattr(self, call)()
