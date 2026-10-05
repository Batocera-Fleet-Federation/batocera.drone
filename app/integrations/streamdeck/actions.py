"""Typed built-in actions resolved by stable ID.

Profiles store ``{"action_type": "builtin", "action_id": "exit-game"}`` --
never a command. The registry maps the ID to a reviewed handler; any trusted
Batocera command a handler needs is encapsulated in ``BatoceraControl`` and
run through ``ProcessRunner`` (argument arrays, no shell). Every action
reports availability for the current ``ActionContext`` so unsupported actions
are disabled/explained instead of silently doing nothing.
"""

import shutil
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .game_runtime import GameRuntime
from .logs import log_event
from .process import ProcessRunner


@dataclass
class ActionContext:
    """Everything an action may want to know; values are optional by design."""

    active_game: Optional[dict] = None
    system: str = ""
    emulator: str = ""
    core: str = ""
    rom: str = ""
    frontend_state: str = ""
    device_id: str = ""
    profile_id: str = ""
    key_index: Optional[int] = None
    trigger: str = "button"
    confirmed: bool = False
    requested_by: str = ""
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def for_game(cls, active_game: Optional[dict], **values) -> "ActionContext":
        game = active_game or {}
        return cls(
            active_game=active_game,
            system=str(game.get("system") or ""),
            emulator=str(game.get("emulator") or ""),
            core=str(game.get("core") or ""),
            rom=str(game.get("rom_path") or ""),
            frontend_state="in-game" if active_game else "frontend",
            **values,
        )


def _available(reason: str = "") -> dict:
    return {"available": True, "reason": reason}


def _unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason}


@dataclass(frozen=True)
class BuiltInAction:
    id: str
    display_name: str
    description: str
    category: str
    default_icon: str
    handler: Callable[[ActionContext], dict]
    availability_handler: Callable[[ActionContext], dict] = lambda _context: _available()
    dangerous: bool = False
    confirmation_required: bool = False
    compatibility: str = "All Batocera systems"
    default_label: str = ""

    def availability(self, context: ActionContext) -> dict:
        try:
            return self.availability_handler(context)
        except Exception as error:  # noqa: BLE001 - availability must never raise
            return _unavailable(f"availability check failed: {error}")

    def validate(self, context: ActionContext) -> dict:
        availability = self.availability(context)
        if not availability.get("available"):
            return availability
        if self.confirmation_required and not context.confirmed:
            return _unavailable("This system action must be confirmed (hold the key, or confirm in the web UI).")
        return availability

    def execute(self, context: ActionContext) -> dict:
        availability = self.availability(context)
        if not availability.get("available"):
            return {"status": "unavailable", "error": availability.get("reason") or "action unavailable"}
        if self.confirmation_required and not context.confirmed:
            return {"status": "confirmation-required", "error": "This system action must be confirmed before it runs."}
        try:
            result = self.handler(context) or {}
        except Exception as error:  # noqa: BLE001 - structured error instead of a crash
            return {"status": "error", "error": str(error) or error.__class__.__name__}
        result.setdefault("status", "ok")
        return result

    def describe(self, context: ActionContext) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category,
            "default_icon": self.default_icon,
            "default_label": self.default_label or self.display_name.upper(),
            "dangerous": self.dangerous,
            "confirmation_required": self.confirmation_required,
            "compatibility": self.compatibility,
            "availability": self.availability(context),
        }


class BatoceraControl:
    """The only place trusted Batocera commands for built-ins are spelled out."""

    SWISSKNIFE = "batocera-es-swissknife"
    AUDIO = "batocera-audio"

    def __init__(self, runner: Optional[ProcessRunner] = None,
                 which: Callable[[str], Optional[str]] = shutil.which,
                 restart_frontend: Optional[Callable[[], bool]] = None) -> None:
        self.runner = runner or ProcessRunner()
        self.which = which
        self._restart_frontend = restart_frontend

    def _run(self, candidates: List[tuple], *, timeout: float = 30.0, success_codes: tuple = (0,)) -> dict:
        for executable, arguments in candidates:
            path = self.which(executable)
            if not path:
                continue
            result = self.runner.run(path, arguments, timeout=timeout)
            log_event("builtin-command", executable=executable, exit_code=result.exit_code,
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


class RetroArchControl:
    """Commands for RetroArch's own UDP command interface, only when it is enabled.

    Batocera may or may not enable ``network_cmd_enable``; availability reads the
    active RetroArch process's config files instead of assuming, and every
    command first verifies RetroArch reports loaded content (``GET_STATUS``).
    """

    PROCESS_NAMES = {"retroarch", "retroarch32"}

    def __init__(self, proc_root: Path = Path("/proc"), host: str = "127.0.0.1") -> None:
        self.proc_root = proc_root
        self.host = host

    def _retroarch_args(self) -> Optional[List[str]]:
        try:
            entries = [entry for entry in self.proc_root.iterdir() if entry.name.isdigit()]
        except OSError:
            return None
        for entry in entries:
            try:
                parts = [part.decode("utf-8", "replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
            except OSError:
                continue
            if parts and Path(parts[0]).name in self.PROCESS_NAMES:
                return parts
        return None

    def command_port(self) -> tuple:
        """(port, reason): port is ``None`` when commands cannot be sent."""
        args = self._retroarch_args()
        if args is None:
            return None, "The active emulator is not RetroArch; this action needs RetroArch's command interface."
        configs: List[str] = []
        for index, argument in enumerate(args[:-1]):
            if argument in ("--config", "-c", "--appendconfig"):
                configs.extend(item for item in args[index + 1].split("|") if item)
        values: Dict[str, str] = {}
        for config in configs:
            try:
                for line in Path(config).read_text(encoding="utf-8", errors="replace").splitlines():
                    key, separator, value = line.partition("=")
                    if separator:
                        values[key.strip()] = value.strip().strip('"')
            except OSError:
                continue
        if values.get("network_cmd_enable", "false").lower() != "true":
            return None, "RetroArch's network command interface (network_cmd_enable) is disabled for this game."
        try:
            port = int(values.get("network_cmd_port", "55355"))
        except ValueError:
            return None, "RetroArch's network_cmd_port is invalid."
        if not 1 <= port <= 65535:
            return None, "RetroArch's network_cmd_port is invalid."
        return port, ""

    def availability(self, context: ActionContext) -> dict:
        if not context.active_game:
            return _unavailable("No game is currently running.")
        port, reason = self.command_port()
        return _available() if port else _unavailable(reason)

    def send(self, command: str) -> dict:
        port, reason = self.command_port()
        if not port:
            return {"status": "unavailable", "error": reason}
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1.0)
                sock.connect((self.host, port))
                sock.send(b"GET_STATUS\n")
                reply = sock.recv(4096)
                if not reply.startswith((b"GET_STATUS PLAYING", b"GET_STATUS PAUSED")):
                    return {"status": "unavailable", "error": "RetroArch reports no loaded content."}
                sock.send(command.encode("ascii") + b"\n")
        except OSError as error:
            return {"status": "error", "error": f"RetroArch did not answer its command interface: {error}"}
        return {"status": "ok", "message": f"Sent {command} to RetroArch."}


class UnknownActionError(KeyError):
    pass


class BuiltInActionRegistry:
    def __init__(self, control: Optional[BatoceraControl] = None, retroarch: Optional[RetroArchControl] = None,
                 game_runtime: Optional[GameRuntime] = None) -> None:
        self.control = control or BatoceraControl()
        self.retroarch = retroarch or RetroArchControl()
        self.game_runtime = game_runtime or GameRuntime()
        self._actions: Dict[str, BuiltInAction] = {}
        self._register_defaults()

    def register(self, action: BuiltInAction) -> None:
        if action.id in self._actions:
            raise ValueError(f"built-in action already registered: {action.id}")
        self._actions[action.id] = action

    def resolve(self, action_id: str) -> BuiltInAction:
        try:
            return self._actions[str(action_id)]
        except KeyError as error:
            raise UnknownActionError(f"unknown built-in action: {action_id}") from error

    def ids(self) -> List[str]:
        return list(self._actions)

    def context(self, **values) -> ActionContext:
        return ActionContext.for_game(self.game_runtime.get_active_game(), **values)

    def list(self, context: Optional[ActionContext] = None) -> List[dict]:
        context = context or self.context(trigger="ui")
        return [action.describe(context) for action in self._actions.values()]

    def availability(self, action_id: str, context: Optional[ActionContext] = None) -> dict:
        return self.resolve(action_id).availability(context or self.context())

    def execute(self, action_id: str, context: Optional[ActionContext] = None) -> dict:
        context = context or self.context()
        try:
            action = self.resolve(action_id)
        except UnknownActionError as error:
            log_event("builtin-action", action_id=action_id, status="error", error="unknown action")
            return {"status": "error", "error": str(error.args[0])}
        started = time.monotonic()
        result = action.execute(context)
        log_event(
            "builtin-action", action_id=action.id, status=result.get("status"), trigger=context.trigger,
            device_id=context.device_id, key=context.key_index, requested_by=context.requested_by,
            error=result.get("error"), duration=time.monotonic() - started,
        )
        return result

    # -- the initial ten ------------------------------------------------------
    def _game_running(self, context: ActionContext) -> dict:
        if context.active_game or self.game_runtime.is_game_running():
            return _available()
        return _unavailable("No game is currently running.")

    def _tool(self, *executables: str) -> Callable[[ActionContext], dict]:
        def check(_context: ActionContext) -> dict:
            if self.control.has(*executables):
                return _available()
            return _unavailable(f"Required Batocera tool not found: {executables[0]}")
        return check

    def _register_defaults(self) -> None:
        control, retroarch = self.control, self.retroarch
        game_tool = self._tool(BatoceraControl.SWISSKNIFE)

        def exit_availability(context: ActionContext) -> dict:
            running = self._game_running(context)
            return running if not running["available"] else game_tool(context)

        state_compat = "RetroArch (libretro) cores with network commands enabled"
        defaults = [
            BuiltInAction("exit-game", "Exit Current Game", "Close the running game and return to EmulationStation.",
                          "Game", "exit", lambda _c: control.exit_game(), exit_availability, default_label="EXIT"),
            BuiltInAction("reboot-system", "Reboot Batocera", "Restart this Batocera machine.", "System", "reboot",
                          lambda _c: control.reboot(), self._tool(BatoceraControl.SWISSKNIFE, "reboot"),
                          dangerous=True, confirmation_required=True, default_label="REBOOT"),
            BuiltInAction("shutdown-system", "Shut Down Batocera", "Power off this Batocera machine.", "System", "power",
                          lambda _c: control.shutdown(), self._tool(BatoceraControl.SWISSKNIFE, "poweroff"),
                          dangerous=True, confirmation_required=True, default_label="POWER"),
            BuiltInAction("restart-emulationstation", "Restart EmulationStation", "Restart the Batocera frontend.",
                          "System", "restart", lambda _c: control.restart_frontend(),
                          dangerous=True, confirmation_required=True, default_label="RESTART"),
            BuiltInAction("volume-up", "Volume Up", "Raise the system volume by 5%.", "Audio", "volume-up",
                          lambda _c: control.volume("+5"), self._tool(BatoceraControl.AUDIO, "amixer"), default_label="VOL +"),
            BuiltInAction("volume-down", "Volume Down", "Lower the system volume by 5%.", "Audio", "volume-down",
                          lambda _c: control.volume("-5"), self._tool(BatoceraControl.AUDIO, "amixer"), default_label="VOL -"),
            BuiltInAction("mute-toggle", "Mute / Unmute", "Toggle system audio mute.", "Audio", "mute",
                          lambda _c: control.volume("mute-toggle"), self._tool(BatoceraControl.AUDIO, "amixer"), default_label="MUTE"),
            BuiltInAction("pause-toggle", "Pause / Resume Current Game", "Pause or resume the running game.", "Game", "pause",
                          lambda _c: retroarch.send("PAUSE_TOGGLE"), retroarch.availability,
                          compatibility=state_compat, default_label="PAUSE"),
            BuiltInAction("save-state", "Save State", "Save the running game to the current state slot.", "State", "save",
                          lambda _c: retroarch.send("SAVE_STATE"), retroarch.availability,
                          compatibility=state_compat, default_label="SAVE"),
            BuiltInAction("load-state", "Load State", "Load the current state slot into the running game.", "State", "load",
                          lambda _c: retroarch.send("LOAD_STATE"), retroarch.availability,
                          compatibility=state_compat, default_label="LOAD"),
        ]
        for action in defaults:
            self.register(action)
