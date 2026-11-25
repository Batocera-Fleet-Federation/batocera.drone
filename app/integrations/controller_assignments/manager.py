"""Controller Assignments integration: lifecycle, status, and rule editing.

Everything here configures *this* machine only. The reordering itself happens
in ``launch_order.py``, copied to ``bin/drone-controller-order`` and run by
EmulationStation in front of the normal launch command; this manager owns the
rules it reads (``state/runtime.json``) and the EmulationStation overlay that
puts it in front of each system's command.
"""

import logging
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..registry import Integration, IntegrationDescriptor
from . import es_overlay
from .config import ConfigStore, compile_runtime, preset_catalog, profile_catalog, type_catalog
from .devices import connected_controllers
from .launch_order import read_batocera_pins
from .paths import ControllerAssignmentsPaths, default_integration_root

try:
    from ..shared.files import atomic_write_bytes, atomic_write_json, read_json
except ImportError:  # pragma: no cover - flat execution
    from integrations.shared.files import atomic_write_bytes, atomic_write_json, read_json  # type: ignore


LOGGER = logging.getLogger("drone.controller_assignments")
WRAPPER_SOURCE = Path(__file__).with_name("launch_order.py")

DESCRIPTOR = IntegrationDescriptor(
    id="controller-assignments",
    name="Controller Assignments",
    description="Choose which controllers become Players 1-8 for each system or game, with ready-made presets "
                "for GameCube, N64, SNES, NES, Sega, PlayStation, Xbox and arcade systems.",
    icon="bi-joystick",
    configure_route="#admin/integrations/controller-assignments",
    capabilities=("player-order", "per-system-rules", "per-game-rules", "presets"),
    documentation="EmulationStation still chooses the controllers; the Drone reorders the ones it passes to "
                  "a game. Batocera's own player assignments always win, and rules only fill open slots. "
                  "Light guns are never moved. Applies only to this machine.",
)


def log_event(event: str, **fields: Any) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    LOGGER.info("[controller-assignments] %s %s", event, details)


class ControllerAssignmentsIntegration(Integration):
    descriptor = DESCRIPTOR

    def __init__(self, settings: Any, repository: Any, *, paths: Optional[ControllerAssignmentsPaths] = None,
                 controller_detector: Callable[[], List[dict]] = connected_controllers,
                 proc_root: Path = Path("/proc")) -> None:
        self.settings = settings
        self.repository = repository
        self.paths = paths or ControllerAssignmentsPaths.default()
        self.expected_root = self.paths.root if paths is not None else default_integration_root()
        self.config = ConfigStore(self.paths)
        self.controller_detector = controller_detector
        self.proc_root = Path(proc_root)
        self._lock = threading.Lock()

    @property
    def marker_file(self) -> Path:
        return self.paths.state_dir / "overlay.json"

    # ------------------------------------------------------------ plumbing
    def _write_runtime(self, config: Optional[dict] = None) -> None:
        config = config or self.config.load()
        runtime = compile_runtime(config, es_settings_file=Path(self.settings.es_settings_file),
                                  roms_root=Path(self.settings.roms_root), report_file=self.paths.report_file)
        self.paths.ensure(self.paths.state_dir)
        atomic_write_json(self.paths.assert_owned(self.paths.runtime_file), runtime, mode=0o644)

    def _write_wrapper(self) -> None:
        self.paths.ensure(self.paths.bin_dir)
        atomic_write_bytes(self.paths.assert_owned(self.paths.wrapper), WRAPPER_SOURCE.read_bytes(), mode=0o755)

    def _wrapper_current(self) -> bool:
        try:
            return self.paths.wrapper.read_bytes() == WRAPPER_SOURCE.read_bytes()
        except OSError:
            return False

    def _apply_overlay(self) -> dict:
        return es_overlay.apply(self.settings, self.paths.wrapper, self.paths.runtime_file, self.marker_file)

    def _after_change(self, config: dict) -> None:
        if self.paths.root.is_dir():
            self._write_runtime(config)

    # -------------------------------------------------------------- status
    def _health(self, enabled: bool, applied: bool, in_sync: bool, restart: bool, wrapper_ok: bool) -> tuple:
        if not enabled:
            return "disabled", "Disabled. EmulationStation's normal player order is used."
        if not applied or not wrapper_ok:
            return "degraded", "Enabled, but the launch override is missing. Use Repair."
        if restart:
            return "waiting", "Restart EmulationStation once so it starts using the Drone's player order."
        if not in_sync:
            return "degraded", "EmulationStation's system files changed since the override was written. Use Repair."
        return "healthy", "Controller order rules apply to every launch."


    def _state(self) -> dict:
        config = self.config.load()
        applied = es_overlay.is_ours(self.settings)
        in_sync = es_overlay.in_sync(self.settings, self.paths.wrapper, self.paths.runtime_file) if applied else False
        restart = es_overlay.restart_needed(self.marker_file, self.proc_root)
        wrapper_ok = self._wrapper_current()
        health, message = self._health(config["enabled"], applied, in_sync, restart, wrapper_ok)
        return {"config": config, "applied": applied, "in_sync": in_sync, "restart": restart,
                "health": health, "message": message}

    def card_status(self) -> dict:
        state = self._state()
        config = state["config"]
        rules = len(config["systems"]) + len(config["games"]) + (1 if config["default"] else 0)
        return {
            "installed": self.paths.root.is_dir(), "enabled": config["enabled"], "health": state["health"],
            "health_message": state["message"], "version": "",
            "metrics": [
                {"label": "Controllers", "value": len(self.controller_detector())},
                {"label": "Rules", "value": rules},
                {"label": "Presets on", "value": len(config["presets"])},
            ],
        }

    def get_status(self) -> dict:
        state = self._state()
        config = state["config"]
        try:
            systems = sorted(es_overlay.effective_commands(self.settings), key=str.lower)
        except OSError:
            systems = []
        pins = read_batocera_pins(self.settings.es_settings_file)
        return {
            "id": DESCRIPTOR.id, "enabled": config["enabled"], "installed": self.paths.root.is_dir(),
            "health": state["health"], "health_message": state["message"],
            "overlay_applied": state["applied"], "overlay_in_sync": state["in_sync"],
            "restart_needed": state["restart"], "wrapped_systems": es_overlay.wrapped_systems(self.settings),
            "overlay_path": str(es_overlay.overlay_path(self.settings)),
            "controllers": self.controller_detector(),
            "batocera_assignments": [{"slot": slot, **pins[slot]} for slot in sorted(pins)],
            "types": type_catalog(),
            "presets": preset_catalog(config["presets"]),
            "auto": config["auto"],
            "profiles": profile_catalog(),
            "default": config["default"],
            "systems_rules": [{"system": key, **value} for key, value in sorted(config["systems"].items())],
            "games_rules": [{"game": key, **value} for key, value in sorted(config["games"].items())],
            "systems": systems,
            "last_launch": read_json(self.paths.report_file, None),
            "paths": {"root": str(self.paths.root), "config": str(self.paths.config_file),
                      "wrapper": str(self.paths.wrapper), "runtime": str(self.paths.runtime_file)},
            "scope": "local-only",
        }

    # ----------------------------------------------------------- lifecycle
    def install(self, progress: Optional[Callable[[str], None]] = None) -> dict:
        self.paths.ensure_layout()
        return {"status": "ok"}

    def enable(self, requested_by: str = "") -> dict:
        with self._lock:
            self.paths.ensure_layout()
            config = self.config.set_enabled(True)
            self._write_wrapper()
            self._write_runtime(config)
            result = self._apply_overlay()
        log_event("enabled", requested_by=requested_by, systems=result["systems"], changed=result["changed"])
        return {"status": "ok", "restart_needed": es_overlay.restart_needed(self.marker_file, self.proc_root),
                "message": f"Controller Assignments enabled for {result['systems']} systems. Restart "
                           "EmulationStation once so it starts using the new launch command."}

    def disable(self, requested_by: str = "") -> dict:
        with self._lock:
            if self.config.exists():
                config = self.config.set_enabled(False)
                self._after_change(config)
            removed = es_overlay.remove(self.settings, self.marker_file) if self.paths.root.is_dir() else \
                {"removed": False}
            if not self.paths.root.is_dir() and es_overlay.is_ours(self.settings):
                es_overlay.overlay_path(self.settings).unlink()
        log_event("disabled", requested_by=requested_by, overlay_removed=removed["removed"])
        return {"status": "disabled", "restart_needed": es_overlay.restart_needed(self.marker_file, self.proc_root),
                "message": "Controller Assignments disabled; rules were kept. Until EmulationStation restarts, "
                           "launches still pass through the Drone but keep the normal order."}

    def repair(self, *, reinstall: bool = False, requested_by: str = "") -> dict:
        with self._lock:
            self.paths.ensure_layout()
            config = self.config.load()
            self._write_wrapper()
            self._write_runtime(config)
            if config["enabled"]:
                result = self._apply_overlay()
                changed = result["changed"]
            else:
                changed = es_overlay.remove(self.settings, self.marker_file)["removed"]
        log_event("repaired", requested_by=requested_by, overlay_changed=changed)
        message = "Controller Assignments repaired."
        if changed:
            message += " Restart EmulationStation to load the updated launch command."
        return {"status": "ok", "restart_needed": es_overlay.restart_needed(self.marker_file, self.proc_root),
                "message": message}

    def remove(self, *, include_configuration: bool = False, requested_by: str = "") -> dict:
        with self._lock:
            es_overlay.remove(self.settings, self.marker_file)
            if es_overlay.restart_needed(self.marker_file, self.proc_root):
                # The running EmulationStation still launches through the
                # wrapper: keep it (pass-through) until it restarts, or every
                # launch would fail.
                if self.config.exists():
                    self._after_change(self.config.set_enabled(False))
                return {"status": "pending-restart", "configuration_removed": False, "removed": [],
                        "restart_needed": True,
                        "message": "The launch override was removed. Restart EmulationStation, then Remove again "
                                   "to delete the Drone's files (games keep launching normally meanwhile)."}
            removed: List[str] = []
            if include_configuration:
                if self.paths.remove_root(self.expected_root):
                    removed = ["config", "state", "bin"]
            else:
                if self.config.exists():
                    self.config.set_enabled(False)
                for name in ("bin", "state"):
                    target = self.paths.owned(name)
                    if target.exists():
                        shutil.rmtree(target)
                        removed.append(name)
        log_event("removed", configuration_removed=include_configuration, requested_by=requested_by)
        message = ("Controller Assignments and its rules were removed." if include_configuration else
                   "Controller Assignments was removed; rules were kept.")
        return {"status": "removed", "configuration_removed": include_configuration, "removed": removed,
                "restart_needed": False, "message": message}

    # ---------------------------------------------------------------- rules
    def _edit(self, mutate: Callable[[], dict], event: str, requested_by: str, **fields: Any) -> dict:
        with self._lock:
            self.paths.ensure(self.paths.config_dir)
            config = mutate()
            self._after_change(config)
        log_event(event, requested_by=requested_by, **fields)
        return {"status": "ok", "message": "Saved. It applies to the next game you launch."}

    def set_auto(self, enabled: Any, requested_by: str = "") -> dict:
        return self._edit(lambda: self.config.set_auto(enabled), "best-effort-updated", requested_by,
                          enabled=enabled is True)

    def set_default(self, order: Any, requested_by: str = "") -> dict:
        return self._edit(lambda: self.config.set_default(order), "default-updated", requested_by)

    def set_preset(self, preset_id: str, enabled: Any, requested_by: str = "") -> dict:
        return self._edit(lambda: self.config.set_preset(preset_id, enabled), "preset-updated", requested_by,
                          preset=preset_id, enabled=enabled is True)

    def set_system(self, system: Any, order: Any, requested_by: str = "") -> dict:
        return self._edit(lambda: self.config.set_system(system, order), "system-rule-updated", requested_by,
                          system=system)

    def delete_system(self, system: Any, requested_by: str = "") -> dict:
        return self._edit(lambda: self.config.delete_system(system), "system-rule-deleted", requested_by,
                          system=system)

    def set_game(self, game: Any, order: Any, name: Any = "", requested_by: str = "") -> dict:
        return self._edit(lambda: self.config.set_game(game, order, name), "game-rule-updated", requested_by)

    def delete_game(self, game: Any, requested_by: str = "") -> dict:
        return self._edit(lambda: self.config.delete_game(game), "game-rule-deleted", requested_by)


_MANAGERS: Dict[str, ControllerAssignmentsIntegration] = {}
_MANAGERS_LOCK = threading.Lock()


def get_controller_assignments_integration(settings: Any, repository: Any) -> ControllerAssignmentsIntegration:
    key = str(ControllerAssignmentsPaths.default().root)
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = ControllerAssignmentsIntegration(settings, repository)
            _MANAGERS[key] = manager
        else:
            manager.settings, manager.repository = settings, repository
        return manager
