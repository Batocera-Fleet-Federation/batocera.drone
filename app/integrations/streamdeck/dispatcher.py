"""Execute one configured button through its typed action path.

Shared by the hardware runtime (physical presses) and the Drone's "Test
Action" so both run identical code. Each action type has exactly one path:
built-ins -> registry, Launch Game -> LaunchGameCoordinator, custom scripts ->
ScriptStore, profile navigation -> the runtime. Nothing here builds commands.
"""

from typing import Callable, Optional

from .actions import ActionContext, BuiltInActionRegistry
from .launch import LaunchGameCoordinator
from .scripts import ScriptStore


class ButtonDispatcher:
    def __init__(self, actions: BuiltInActionRegistry, coordinator: LaunchGameCoordinator, scripts: ScriptStore,
                 script_timeout: Callable[[], float] = lambda: 30.0) -> None:
        self.actions = actions
        self.coordinator = coordinator
        self.scripts = scripts
        self.script_timeout = script_timeout

    def execute(self, button: dict, context: ActionContext,
                navigate: Optional[Callable[[dict], dict]] = None) -> dict:
        action_type = button.get("action_type", "none")
        if action_type == "builtin":
            return self.actions.execute(str(button.get("action_id") or ""), context)
        if action_type == "game":
            game = button.get("game") or {}
            if game.get("installed") is False:
                return {"status": "error", "stage": "validate", "error": "Game not found. Relink this button."}
            return self.coordinator.launch(game, context)
        if action_type == "script":
            env = {
                "DRONE_STREAMDECK_TRIGGER": context.trigger,
                "DRONE_STREAMDECK_KEY": "" if context.key_index is None else str(context.key_index),
                "DRONE_STREAMDECK_DEVICE_ID": context.device_id,
                "DRONE_STREAMDECK_PROFILE_ID": context.profile_id,
                "DRONE_STREAMDECK_GAME_SYSTEM": context.system,
                "DRONE_STREAMDECK_GAME_ROM": context.rom,
            }
            return self.scripts.run(str(button.get("script_id") or ""), timeout=self.script_timeout(),
                                    context=env, requested_by=context.requested_by)
        if action_type == "profile":
            if navigate is None:
                return {"status": "unavailable", "error": "Profile navigation runs on the physical Stream Deck."}
            return navigate(button)
        return {"status": "ignored"}
