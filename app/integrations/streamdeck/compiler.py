"""Compile the saved configuration into what the hardware runtime executes.

The Drone owns the library (ROM cache, gamelists, uploads, scripts); the worker
does not. On Apply/enable/startup the Drone resolves every button -- game IDs
against the current library, artwork to a confined local file, uploads to
their stored original, built-ins to registry metadata -- and writes
``state/runtime.json``. Each key also gets a *render spec*; the browser
preview is built from the very same spec (with file paths swapped for admin
URLs), so what the page shows is what the worker draws.
"""

import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from .actions import BuiltInActionRegistry, UnknownActionError
from .games import GameLibrary
from .images import ImageStore
from .scripts import ScriptStore

API_ROOT = "/v1/api/admin/integrations/streamdeck"
ACTION_BACKGROUNDS = {
    "exit-game": "#b91c1c", "reboot-system": "#7f1d1d", "shutdown-system": "#7f1d1d",
    "restart-emulationstation": "#9a3412", "volume-up": "#065f46", "volume-down": "#065f46",
    "mute-toggle": "#065f46", "pause-toggle": "#1e3a8a", "save-state": "#4c1d95", "load-state": "#4c1d95",
}


def _generated(image: dict, text: str, symbol: str = "", background: Optional[str] = None,
               secondary: str = "") -> dict:
    return {
        "kind": "generated",
        "text": text[:24],
        "secondary_text": secondary[:24],
        "symbol": symbol,
        "background": background or image.get("background") or "#111827",
        "text_color": image.get("text_color") or "#ffffff",
        "text_size": image.get("text_size") or "medium",
        "align": image.get("align") or "middle",
    }


class RuntimeCompiler:
    def __init__(self, games: GameLibrary, images: ImageStore, scripts: ScriptStore,
                 actions: BuiltInActionRegistry) -> None:
        self.games = games
        self.images = images
        self.scripts = scripts
        self.actions = actions

    def _script(self, script_id: str) -> Optional[dict]:
        try:
            return self.scripts.get(script_id, include_code=False)
        except (KeyError, ValueError):
            return None

    def describe_button(self, button: dict, profiles: Dict[str, dict], *, for_ui: bool) -> dict:
        """Resolved action summary + render spec for one configured key."""
        image = button.get("image") or {}
        action_type = button.get("action_type", "none")
        label = button.get("label") or ""
        summary: Dict[str, Any] = {"type": action_type, "problem": ""}
        default_spec: Dict[str, Any] = {"kind": "blank"}
        game = None
        if action_type == "builtin":
            try:
                action = self.actions.resolve(button.get("action_id", ""))
                summary.update(name=action.display_name, category=action.category,
                               dangerous=action.dangerous, confirmation_required=action.confirmation_required)
                default_spec = _generated(image, label or action.default_label or action.display_name.upper(),
                                          action.default_icon, ACTION_BACKGROUNDS.get(action.id))
            except UnknownActionError:
                summary.update(name=button.get("action_id"), problem="Unknown built-in action.")
                default_spec = _generated(image, label or "?", "", "#374151")
        elif action_type == "game":
            game = self.games.resolve(button.get("game") or {})
            summary.update(name=game["name"], system=game["system"], installed=game["installed"],
                           resolution=game.get("resolution"))
            if not game["installed"]:
                summary["problem"] = "Game not found. Relink this button."
            default_spec = self._game_spec(game, image, label, field="auto", for_ui=for_ui)
        elif action_type == "script":
            script = self._script(button.get("script_id", ""))
            summary.update(name=script["name"] if script else "Missing script")
            if not script:
                summary["problem"] = "Script not found. Assign another script."
            default_spec = _generated(image, label or (script["name"] if script else "SCRIPT"), "script", "#374151")
        elif action_type == "profile":
            operation = button.get("operation")
            target = profiles.get(button.get("profile_id", "")) if operation == "go-to" else None
            text = {"next": "NEXT", "previous": "PREV"}.get(operation) or (target["name"] if target else "PROFILE")
            summary.update(name={"next": "Next profile", "previous": "Previous profile"}.get(operation)
                           or f"Go to {target['name'] if target else 'missing profile'}")
            if operation == "go-to" and not target:
                summary["problem"] = "Target profile no longer exists."
            symbol = {"next": "next", "previous": "previous"}.get(operation, "profile")
            default_spec = _generated(image, label or text, symbol, "#0f766e")
        elif label:
            default_spec = _generated(image, label)

        image_type = image.get("type", "default")
        if image_type == "blank":
            spec: Dict[str, Any] = {"kind": "blank"}
        elif image_type == "generated":
            fallback_text = default_spec.get("text", "") if default_spec.get("kind") == "generated" else ""
            spec = _generated(image, image.get("text") or label or fallback_text, image.get("symbol") or "",
                              secondary=image.get("secondary_text") or "")
        elif image_type == "uploaded":
            path = self.images.path(image.get("image_id", ""))
            fallback = _generated(image, label or "IMAGE?", "", "#374151")
            if path is None:
                spec = fallback
                summary["problem"] = summary["problem"] or "Uploaded image is missing."
            else:
                spec = {"kind": "image", "fit": image.get("fit") or "fill", "background": image.get("background") or "#000000",
                        "text": label, "text_color": image.get("text_color") or "#ffffff", "fallback": fallback}
                if for_ui:
                    spec["source_url"] = f"{API_ROOT}/images/{image['image_id']}"
                else:
                    spec["source"] = str(path)
        elif image_type == "game-artwork" and game is not None:
            spec = self._game_spec(game, image, label, field=image.get("artwork_field") or "auto", for_ui=for_ui)
        else:
            spec = default_spec
        resolved = None
        if game is not None:
            resolved = {key: game.get(key) for key in ("id", "name", "system", "rom_path", "installed", "resolution", "favorite")}
        return {"summary": summary, "render": spec, "resolved_game": resolved}

    def _game_spec(self, game: dict, image: dict, label: str, *, field: str, for_ui: bool) -> dict:
        fallback = _generated(image, label or game.get("name", "GAME"), "game", "#1f2937", secondary=game.get("system", ""))
        if not game.get("installed"):
            return fallback
        artwork = self.games.artwork_path(game, field)
        if artwork is None:
            return fallback
        spec = {"kind": "image", "fit": image.get("fit") or "fill", "background": image.get("background") or "#000000",
                "text": label, "text_color": image.get("text_color") or "#ffffff", "fallback": fallback}
        if for_ui:
            spec["source_url"] = f"{API_ROOT}/games/artwork?" + urlencode(
                {"system": game["system"], "rom_path": game["rom_path"], "field": field})
        else:
            spec["source"] = str(artwork)
        return spec

    def profile_view(self, config: dict) -> list:
        profiles = {profile["id"]: profile for profile in config["profiles"]}
        views = []
        for profile in config["profiles"]:
            buttons = []
            for button in profile["buttons"]:
                described = self.describe_button(button, profiles, for_ui=True)
                buttons.append({**button, **described})
            views.append({"id": profile["id"], "name": profile["name"], "buttons": buttons,
                          "is_default": profile["id"] == config["default_profile_id"]})
        return views

    def compile(self, config: dict) -> dict:
        profiles = {profile["id"]: profile for profile in config["profiles"]}
        compiled_profiles = []
        for profile in config["profiles"]:
            buttons = []
            for button in profile["buttons"]:
                described = self.describe_button(button, profiles, for_ui=False)
                entry = {key: button[key] for key in ("key", "action_type", "label", "action_id",
                                                      "script_id", "operation", "profile_id") if key in button}
                if described["resolved_game"] is not None:
                    entry["game"] = described["resolved_game"]
                entry["render"] = described["render"]
                entry["problem"] = described["summary"].get("problem", "")
                buttons.append(entry)
            compiled_profiles.append({"id": profile["id"], "name": profile["name"], "buttons": buttons})
        return {
            "generation": time.time_ns(),
            "compiled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "enabled": config["enabled"],
            "settings": dict(config["settings"]),
            "default_profile_id": config["default_profile_id"],
            "profiles": compiled_profiles,
            "devices": list(config["devices"]),
            "context_rules": list(config["context_rules"]),
        }
