"""Media Keys integration: lifecycle, status, bindings, and binding tests.

Everything here configures *this* machine only. The key presses themselves are
handled by ``dispatch.py`` (spawned by hotkeygen), not by the Drone process;
this manager owns the config the dispatcher reads and the hotkeygen override
that points keys at it.
"""

import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..registry import Integration, IntegrationDescriptor
from .config import (ConfigStore, MANAGED_KEYS, builtin_catalog, hold_identity, hold_seconds, requires_hold)
from .dispatch import (HoldGate, execute_binding, log_media_key, media_keys_script_store,
                       record_press, script_owner)
from .hotkeygen import HotkeygenOverrides, detect_keyboards
from .paths import MediaKeysPaths, default_integration_root

try:
    from ..shared.batocera_control import BUILTIN_COMMANDS, BatoceraControl
    from ..shared.files import read_json
    from ..shared.process import ProcessRunner
except ImportError:  # pragma: no cover - flat execution
    from integrations.shared.batocera_control import BUILTIN_COMMANDS, BatoceraControl  # type: ignore
    from integrations.shared.files import read_json  # type: ignore
    from integrations.shared.process import ProcessRunner  # type: ignore


DESCRIPTOR = IntegrationDescriptor(
    id="media-keys",
    name="Media Keys",
    description="Bind USB keyboard media keys (volume, mute, exit game, reboot) to built-in actions or your own "
                "scripts instead of Batocera's stock hotkey defaults.",
    icon="bi-keyboard-fill",
    configure_route="#admin/integrations/media-keys",
    capabilities=("key-bindings", "built-in-actions", "custom-scripts", "diagnostics"),
    documentation="Batocera's hotkeygen keeps reading the keyboard; the Drone adds one override file that routes "
                  "the managed keys to its dispatcher. Disabling restores the stock behaviour. Applies only to "
                  "this machine.",
)


class MediaKeysIntegration(Integration):
    descriptor = DESCRIPTOR

    def __init__(self, settings: Any, repository: Any, *, paths: Optional[MediaKeysPaths] = None,
                 overrides: Optional[HotkeygenOverrides] = None, control: Optional[BatoceraControl] = None,
                 runner: Optional[ProcessRunner] = None,
                 keyboard_detector: Callable[[], List[dict]] = detect_keyboards) -> None:
        self.settings = settings
        self.repository = repository
        self.paths = paths or MediaKeysPaths.default()
        self.expected_root = self.paths.root if paths is not None else default_integration_root()
        self.runner = runner or ProcessRunner()
        self.config = ConfigStore(self.paths)
        self.scripts = media_keys_script_store(self.paths, self.runner)
        self.overrides = overrides or HotkeygenOverrides(self.paths)
        self.control = control or BatoceraControl(self.runner, log=log_media_key)
        self.keyboard_detector = keyboard_detector
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ status
    def _health(self, enabled: bool, applied: bool, running: bool) -> tuple:
        if not enabled:
            return "disabled", "Disabled. Batocera's stock media-key behaviour is active."
        if not applied:
            return "degraded", "Enabled, but the hotkeygen override is missing. Use Repair."
        if not running:
            return "degraded", "Override applied, but hotkeygen does not appear to be running."
        return "healthy", "Media keys are routed to the Drone."

    def card_status(self) -> dict:
        config = self.config.load()
        applied = self.overrides.applied()
        running = self.overrides.hotkeygen_running()
        health, message = self._health(config["enabled"], applied, running)
        return {
            "installed": self.paths.root.is_dir(), "enabled": config["enabled"], "health": health,
            "health_message": message, "version": "",
            "metrics": [
                {"label": "Keyboards", "value": len(self.keyboard_detector())},
                {"label": "Overrides", "value": "Applied" if applied else "Not applied"},
                {"label": "Managed keys", "value": len(MANAGED_KEYS)},
            ],
        }

    def bindings_view(self, config: Optional[dict] = None) -> List[dict]:
        config = config or self.config.load()
        presses = read_json(self.paths.press_file, {}) or {}
        rows = []
        for binding_id, (key_name, label) in MANAGED_KEYS.items():
            binding = config["bindings"].get(binding_id, {"action_type": "none"})
            row = {"id": binding_id, "key": key_name, "label": label, **binding,
                   "requires_hold": requires_hold(binding) if binding.get("action_type") != "none" else False,
                   "last_press": presses.get(binding_id) if isinstance(presses, dict) else None}
            if requires_hold(binding) and binding.get("action_type") != "none":
                row["hold_seconds"] = hold_seconds(binding)
            if binding.get("action_type") == "script":
                try:
                    row["script_name"] = self.scripts.get(binding["script_id"], include_code=False)["name"]
                except (KeyError, ValueError):
                    row["script_name"] = ""
                    row["script_missing"] = True
            rows.append(row)
        return rows

    def get_status(self) -> dict:
        config = self.config.load()
        applied = self.overrides.applied()
        running = self.overrides.hotkeygen_running()
        health, message = self._health(config["enabled"], applied, running)
        return {
            "id": DESCRIPTOR.id, "enabled": config["enabled"], "installed": self.paths.root.is_dir(),
            "health": health, "health_message": message,
            "hotkeygen_running": running, "overrides_applied": applied,
            "override_path": str(self.overrides.override_path),
            "keyboards": self.keyboard_detector(),
            "bindings": self.bindings_view(config),
            "builtins": builtin_catalog(),
            "paths": {"root": str(self.paths.root), "scripts": str(self.paths.scripts_dir),
                      "config": str(self.paths.config_file), "launcher": str(self.paths.launcher)},
            "scope": "local-only",
        }

    # --------------------------------------------------------------- lifecycle
    def install(self, progress: Optional[Callable[[str], None]] = None) -> dict:
        self.paths.ensure_layout()
        return {"status": "ok"}

    def enable(self, requested_by: str = "") -> dict:
        with self._lock:
            self.paths.ensure_layout()
            applied = self.overrides.apply()
            self.config.set_enabled(True)
        log_media_key("integration-enabled", requested_by=requested_by, override_changed=applied["changed"])
        return {"status": "ok", "message": "Media Keys enabled. New hotkeygen mappings apply when hotkeygen "
                                           "re-reads its configuration (restart it or reboot if keys do not react)."}

    def disable(self, requested_by: str = "") -> dict:
        with self._lock:
            if self.config.exists():
                self.config.set_enabled(False)
            restored = self.overrides.restore()
        log_media_key("integration-disabled", requested_by=requested_by, override_removed=restored["removed"])
        return {"status": "disabled", "message": "Media Keys disabled. Stock key behaviour restored; bindings "
                                                 "and scripts were kept."}

    def repair(self, *, reinstall: bool = False, requested_by: str = "") -> dict:
        with self._lock:
            self.paths.ensure_layout()
            if self.config.load()["enabled"]:
                self.overrides.apply()
            else:
                self.overrides.restore()
        log_media_key("integration-repaired", requested_by=requested_by)
        return {"status": "ok", "message": "Media Keys repaired."}

    def remove(self, *, include_configuration: bool = False, requested_by: str = "") -> dict:
        with self._lock:
            self.overrides.restore()
            removed: List[str] = []
            if include_configuration:
                if self.paths.remove_root(self.expected_root):
                    removed = ["config", "scripts", "state", "logs", "bin"]
            else:
                if self.config.exists():
                    self.config.set_enabled(False)
                for name in ("bin", "state", "logs"):
                    target = self.paths.owned(name)
                    if target.exists():
                        shutil.rmtree(target)
                        removed.append(name)
        log_media_key("integration-removed", configuration_removed=include_configuration, requested_by=requested_by)
        message = ("Media Keys and its configuration were removed." if include_configuration else
                   "Media Keys was removed; bindings and scripts were kept.")
        return {"status": "removed", "configuration_removed": include_configuration, "removed": removed,
                "message": message}

    # ---------------------------------------------------------------- bindings
    def set_binding(self, binding_id: str, payload: dict, requested_by: str = "") -> dict:
        if isinstance(payload, dict) and payload.get("action_type") == "script":
            self.scripts.get(payload.get("script_id") if payload.get("script_id") else "")  # must exist
        self.config.set_binding(binding_id, payload)
        log_media_key("binding-updated", binding=binding_id, requested_by=requested_by)
        return {"status": "ok", "binding": next(row for row in self.bindings_view() if row["id"] == binding_id)}

    def script_references(self, script_id: str) -> List[dict]:
        return self.config.script_references(script_id)

    def test_binding(self, binding_id: str, payload: Optional[dict] = None, requested_by: str = "") -> dict:
        """Run a binding's action now. Reboot/shutdown need an explicit ``confirm``."""
        if binding_id not in MANAGED_KEYS:
            raise KeyError("unknown key binding")
        binding = self.config.load()["bindings"].get(binding_id, {"action_type": "none"})
        if binding.get("action_type") == "none":
            raise ValueError("this key has no action assigned")
        confirm_needed = binding.get("action_type") == "builtin" and BUILTIN_COMMANDS[binding["action_id"]][2]
        if confirm_needed:
            if (payload or {}).get("confirm") is not True:
                raise ValueError("this action needs confirmation")
            gate = HoldGate(self.paths).check(binding_id, hold_seconds(binding),
                                              hold_identity(self.config.load(), binding),
                                              origin="test")
            if gate != "fire":
                return {"status": gate, "message": "Keep holding to confirm this action."}
        run_as = script_owner(self.paths) if binding.get("action_type") == "script" else None
        result = execute_binding(binding, self.paths, control=self.control, scripts=self.scripts,
                                 requested_by=f"test:{requested_by}"[:80], run_as=run_as)
        record_press(self.paths, binding_id, result)
        log_media_key("binding-tested", binding=binding_id, status=result.get("status"), requested_by=requested_by)
        return result


_MANAGERS: Dict[str, MediaKeysIntegration] = {}
_MANAGERS_LOCK = threading.Lock()


def get_media_keys_integration(settings: Any, repository: Any) -> MediaKeysIntegration:
    key = str(MediaKeysPaths.default().root)
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = MediaKeysIntegration(settings, repository)
            _MANAGERS[key] = manager
        else:
            manager.settings, manager.repository = settings, repository
        return manager
