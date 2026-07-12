"""RomRequestHandler admin handlers for EmulationStation collections/systems-
displayed/grouped-systems and music volume, as a mixin.

Composed onto ``RomRequestHandler``; delegates all real work to
``device/es_collections.py`` (shared with the Overmind remote-control actions
in ``overmind/actions.py`` so the local admin UI and Overmind apply changes
through the exact same code path).
"""

try:
    from ..device.es_collections import apply_es_collections as _apply_es_collections
    from ..device.es_collections import get_es_collections_state as _get_es_collections_state
except ImportError:  # pragma: no cover - direct script execution fallback
    from device.es_collections import apply_es_collections as _apply_es_collections  # type: ignore
    from device.es_collections import get_es_collections_state as _get_es_collections_state  # type: ignore


class HandlersEsCollectionsMixin:
    def _handle_admin_es_collections_get(self) -> None:
        try:
            state = _get_es_collections_state(self.settings)
        except Exception as error:
            self._send_json(500, {"error": f"Unable to read EmulationStation collections state: {error}"})
            return
        self._send_json(200, state)

    def _handle_admin_es_collections_post(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        updates = {
            key: payload[key]
            for key in ("music_volume", "screensaver_minutes", "hidden_systems", "ungrouped_systems", "auto_collections", "custom_collections")
            if key in payload
        }
        if not updates:
            self._send_json(400, {"error": "No recognized collections fields were provided"})
            return
        try:
            state = _apply_es_collections(self.settings, updates)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        except Exception as error:
            self._send_json(500, {"error": f"Unable to update EmulationStation collections: {error}"})
            return
        self._send_json(200, state)

    def _handle_admin_music_volume_post(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        try:
            level = int(payload.get("level"))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "A numeric music volume level from 0 to 100 is required"})
            return
        if level < 0 or level > 100:
            self._send_json(400, {"error": "Music volume must be from 0 to 100"})
            return
        try:
            state = _apply_es_collections(self.settings, {"music_volume": level})
        except Exception as error:
            self._send_json(500, {"error": f"Unable to set music volume: {error}"})
            return
        self._send_json(200, state)
