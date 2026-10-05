"""Drone-side Stream Deck integration manager (stdlib only).

Owns everything that is not hardware I/O: tooling lifecycle, configuration,
compiling ``state/runtime.json``, supervising the isolated worker process,
device discovery for the UI, diagnostics, and admin-initiated tests. The
worker (``worker.py``) owns the decks themselves.

Lifecycle summary:

* enable  -- job: verify/install isolated tooling (idempotent), create the
  default configuration, compile, start the worker, wait for it to connect.
* disable -- stop the worker (devices released), keep config/profiles/
  images/scripts.
* repair  -- job: verify the installed libraries (reinstall only if broken),
  recover malformed config, recompile, restart and reconnect.
* reinstall tooling -- job: remove and reinstall only ``python/``+``lib/``.
* remove  -- tooling only, or tooling + configuration (the whole validated
  integration directory). Nothing outside it is touched; system pip/
  setuptools are never installed or removed.

The supervisor thread (started from ``create_server``) restores the runtime
after a reboot/Drone restart and restarts a crashed worker with backoff.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..registry import Integration, IntegrationDescriptor
from .actions import ActionContext, BatoceraControl, BuiltInActionRegistry, RetroArchControl, UnknownActionError
from .compiler import RuntimeCompiler
from .config import ConfigStore, image_references, safe_id, script_references, validate_game
from .dependencies import STREAMDECK_VERSION, DependencyError, DependencyManager
from .devices import detect_usb_devices, preferred_serial, same_physical_device
from .dispatcher import ButtonDispatcher
from .emulationstation import EmulationStationApi
from .game_launcher import GameLauncher
from .game_runtime import GameRuntime
from .games import GameLibrary
from .images import ImageStore
from .jobs import JobRegistry
from .launch import LaunchGameCoordinator
from .logs import log_event
from .paths import CONTENT_DIRS, TOOLING_DIRS, StreamDeckPaths, atomic_write_json, lock_is_held, read_json
from .process import ProcessRunner
from .scripts import ScriptStore


DESCRIPTOR = IntegrationDescriptor(
    id="streamdeck",
    name="Stream Deck",
    description="Use Elgato Stream Deck keys on this Batocera machine for built-in controls, one-press game "
                "launching, profiles, and custom scripts.",
    icon="bi-grid-3x3-gap-fill",
    configure_route="#admin/integrations/streamdeck",
    capabilities=("devices", "profiles", "built-in-actions", "launch-game", "custom-scripts", "images", "diagnostics"),
    documentation="Installs its own isolated libraries under the Drone folder and runs a local hardware runtime. "
                  "Configuration applies only to this machine.",
)

LIFECYCLE_JOBS = ["enable", "repair", "reinstall"]
SUPERVISOR_SECONDS = 10.0
RESTART_BACKOFF_MAX = 300.0
AUTO_REPAIR_INTERVAL = 1800.0
CONNECT_WAIT_SECONDS = 15.0
# Disable/remove/tests wait this long for a supervisor pass (seconds) before
# reporting that an install or repair holds the lifecycle lock.
LIFECYCLE_LOCK_WAIT_SECONDS = 15.0
_WORKER_MODULE = "app.integrations.streamdeck.worker"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _later(*events: Optional[dict]) -> Optional[dict]:
    present = [event for event in events if isinstance(event, dict)]
    return max(present, key=lambda event: str(event.get("at") or "")) if present else None


class StreamDeckIntegration(Integration):
    descriptor = DESCRIPTOR

    def __init__(
        self,
        settings: Any,
        repository: Any,
        *,
        paths: Optional[StreamDeckPaths] = None,
        runner: Optional[ProcessRunner] = None,
        dependencies: Optional[DependencyManager] = None,
        game_runtime: Optional[GameRuntime] = None,
        control: Optional[BatoceraControl] = None,
        launcher: Optional[GameLauncher] = None,
        python: Optional[str] = None,
        process_factory: Optional[Callable[..., Any]] = None,
        usb_detector: Callable[[], List[dict]] = detect_usb_devices,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.paths = paths or StreamDeckPaths.default()
        # Full removal deletes the integration root only at this exact location
        # (and only if it is <...>/integrations/streamdeck -- see remove_root).
        self.expected_root = self.paths.root if paths is not None else StreamDeckPaths.default().root
        self.python = python or sys.executable
        self.runner = runner or ProcessRunner()
        self.dependencies = dependencies or DependencyManager(self.paths, self.runner, self.python)
        self.config = ConfigStore(self.paths)
        es_api = EmulationStationApi()
        self.game_runtime = game_runtime or GameRuntime(es_api=es_api)
        self.actions = BuiltInActionRegistry(control or BatoceraControl(self.runner), RetroArchControl(), self.game_runtime)
        self.launcher = launcher or GameLauncher(settings.roms_root, es_api)
        self.coordinator = LaunchGameCoordinator(self.game_runtime, self.launcher, self.actions,
                                                 lock_path=self.paths.launch_lock)
        self.scripts = ScriptStore(self.paths, self.runner)
        self.images = ImageStore(self.paths, verifier=self._verify_image)
        self.games = GameLibrary(repository, settings.roms_root)
        self.compiler = RuntimeCompiler(self.games, self.images, self.scripts, self.actions)
        self.dispatcher = ButtonDispatcher(self.actions, self.coordinator, self.scripts,
                                           script_timeout=lambda: float(self.config.load()["settings"]["script_timeout_seconds"]))
        self.jobs = JobRegistry()
        self.process_factory = process_factory or subprocess.Popen
        self.usb_detector = usb_detector
        self._process: Optional[Any] = None
        self._runtime_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._supervisor: Optional[threading.Thread] = None
        self._supervisor_stop = threading.Event()
        self._restart_failures = 0
        self._next_restart_at = 0.0
        self._last_auto_repair = 0.0
        self._manager_events: Dict[str, Any] = {}
        self.lifecycle_lock_wait = LIFECYCLE_LOCK_WAIT_SECONDS

    # ------------------------------------------------------------------ status
    def _record(self, **values: Any) -> None:
        self._manager_events.update(values)

    def worker_status(self) -> dict:
        payload = read_json(self.paths.worker_status_file, {})
        return payload if isinstance(payload, dict) else {}

    def runtime_info(self) -> dict:
        exit_code = None
        with self._runtime_lock:
            if self._process is not None and self._process.poll() is not None:
                exit_code = self._process.returncode
                self._process = None
                self._record(last_runtime_exit={"exit_code": exit_code, "at": _now_iso()})
        running = lock_is_held(self.paths.runtime_lock)
        pid_info = read_json(self.paths.pid_file, {}) if running else {}
        pid = pid_info.get("pid") if isinstance(pid_info, dict) else None
        return {"running": running, "pid": pid if isinstance(pid, int) else None}

    def _health(self, enabled: bool, tooling: dict, runtime: dict, worker: dict, connected: int) -> tuple:
        job = self.jobs.running(LIFECYCLE_JOBS)
        if job:
            return "installing", job.get("message") or "Working..."
        if not enabled:
            return "disabled", "Stream Deck support is disabled."
        if not tooling["installed"]:
            return "error", tooling["reason"]
        if not runtime["running"]:
            return "error", "The Stream Deck runtime is not running; it restarts automatically, or use Repair."
        if worker.get("runtime") == "error":
            return "error", worker.get("last_error") or "The runtime reported an error."
        if not connected:
            return "waiting", "Ready -- waiting for a Stream Deck to be connected."
        return "healthy", f"{connected} Stream Deck{'s' if connected != 1 else ''} connected."

    def card_status(self) -> dict:
        config = self.config.load()
        tooling = self.dependencies.status()
        runtime = self.runtime_info()
        worker = self.worker_status() if runtime["running"] else {}
        connected = len(worker.get("devices") or []) if runtime["running"] else 0
        detected = len(self.usb_detector()) if not connected else connected
        health, message = self._health(config["enabled"], tooling, runtime, worker, connected)
        return {
            "installed": tooling["installed"], "enabled": config["enabled"], "health": health,
            "health_message": message, "version": STREAMDECK_VERSION if tooling["installed"] else "",
            "metrics": [
                {"label": "Connected devices", "value": connected},
                {"label": "Detected on USB", "value": detected},
                {"label": "Runtime", "value": "Running" if runtime["running"] else "Stopped"},
            ],
        }

    def get_status(self) -> dict:
        config = self.config.load()
        tooling = self.dependencies.status()
        runtime = self.runtime_info()
        worker = self.worker_status() if runtime["running"] else {}
        devices = self.devices(worker=worker if runtime["running"] else {})
        connected = [device for device in devices if device.get("source") == "runtime"]
        health, message = self._health(config["enabled"], tooling, runtime, worker, len(connected))
        events = self._manager_events
        active_game = self.game_runtime.get_active_game()
        compiled = read_json(self.paths.runtime_document, {}) or {}
        try:
            pending = self.config.path.stat().st_mtime > self.paths.runtime_document.stat().st_mtime
        except OSError:
            pending = self.config.exists()
        runtime_state = "running" if runtime["running"] else ("error" if config["enabled"] and tooling["installed"] else "stopped")
        return {
            "id": DESCRIPTOR.id,
            "enabled": config["enabled"],
            "installed": tooling["installed"],
            "health": health,
            "health_message": message,
            "version": STREAMDECK_VERSION if tooling["installed"] else "",
            "tooling": "installed" if tooling["installed"] else ("error" if self.dependencies.marker() else "missing"),
            "tooling_reason": tooling["reason"],
            "tooling_method": tooling["method"],
            "hid_transport": tooling["hid_transport"],
            "versions": {"python": sys.version.split()[0], "streamdeck": tooling["versions"].get("streamdeck", ""),
                         "pillow": tooling["versions"].get("pillow", ""),
                         "tooling_python": tooling["versions"].get("python", "")},
            "runtime": runtime_state,
            "runtime_pid": runtime["pid"],
            "runtime_started_at": worker.get("started_at"),
            "device": "connected" if connected else "disconnected",
            "connected_device_count": len(connected),
            "devices": devices,
            "default_profile_id": config["default_profile_id"],
            "active_profiles": {device["id"]: device.get("active_profile_id") for device in connected},
            "active_game": {key: active_game.get(key) for key in ("system", "name", "rom_path", "emulator", "core")}
            if active_game else None,
            "launch_state": (self.coordinator.state if self.jobs.running(["test-game"])
                             else worker.get("launch_state", self.coordinator.state)),
            "last_device_connection": worker.get("last_device_connection"),
            "last_button_press": worker.get("last_button_press"),
            "last_action": _later(worker.get("last_action"), events.get("last_action")),
            "last_action_result": _later(worker.get("last_action_result"), events.get("last_action_result")),
            "last_game_launch": _later(worker.get("last_game_launch"), events.get("last_game_launch")),
            "last_error": worker.get("last_error") or events.get("last_error") or "",
            "last_runtime_exit": events.get("last_runtime_exit"),
            "job": self.jobs.running(LIFECYCLE_JOBS) or self.jobs.latest(LIFECYCLE_JOBS),
            "config_warnings": self.config.last_warnings,
            "config_recovery": self.config.last_recovery,
            "applied_generation": worker.get("applied_generation"),
            "compiled_generation": compiled.get("generation"),
            "pending_apply": bool(pending),
            "settings": config["settings"],
            "paths": {"root": str(self.paths.root), "scripts": str(self.paths.scripts_dir),
                      "images": str(self.paths.images_dir), "logs": str(self.paths.logs_dir),
                      "config": str(self.paths.config_file)},
            "scope": "local-only",
        }

    def _acquire_lifecycle(self, busy_message: str, error: type = RuntimeError) -> None:
        """Take the lifecycle lock for a short user operation.

        Install/repair jobs hold it for minutes, so fail fast while one runs;
        otherwise wait out a supervisor pass (it holds the lock briefly)
        instead of reporting a spurious conflict.
        """
        if self.jobs.running(LIFECYCLE_JOBS) or not self._lifecycle_lock.acquire(timeout=self.lifecycle_lock_wait):
            raise error(busy_message)

    # ----------------------------------------------------------------- devices
    def devices(self, worker: Optional[dict] = None) -> List[dict]:
        if worker is None:
            worker = self.worker_status() if self.runtime_info()["running"] else {}
        config = self.config.load()
        settings_by_id = {row["device_id"]: row for row in config["devices"]}
        known_path = self.paths.state_dir / "known-devices.json"
        known = read_json(known_path, {}) or {}
        if not isinstance(known, dict):
            known = {}
        rows: List[dict] = []
        for device in worker.get("devices") or []:
            if not isinstance(device, dict) or not device.get("id"):
                continue
            if self._existing_device(rows, device) is not None:
                continue
            entry = {**device, "connected": True, "source": "runtime", "runtime_state": "open"}
            rows.append(entry)
            remembered = {key: device.get(key) for key in ("id", "model", "serial", "firmware", "key_count", "rows",
                                                           "columns", "key_image_size", "has_key_images")}
            if known.get(device["id"]) != remembered:
                known[device["id"]] = remembered
                try:
                    atomic_write_json(known_path, known)
                except OSError:
                    pass
        for device in self.usb_detector():
            if not isinstance(device, dict) or not device.get("id"):
                continue
            existing = self._existing_device(rows, device)
            if existing is not None:
                self._merge_usb_into(existing, device)
                continue
            remembered = known.get(device["id"]) or {}
            rows.append({**device, **{k: v for k, v in remembered.items() if v}, "connected": True,
                         "runtime_state": "not-open", "source": "usb"})
        for device_id, remembered in known.items():
            if not isinstance(remembered, dict):
                continue
            candidate = {**remembered, "id": remembered.get("id") or device_id}
            if self._existing_device(rows, candidate) is not None:
                continue
            rows.append({**remembered, "connected": False, "runtime_state": "disconnected", "source": "remembered"})
        for row in rows:
            setting = self._settings_for(row, settings_by_id)
            row["brightness"] = setting.get("brightness", 60)
            row["startup_profile_id"] = setting.get("startup_profile_id", "")
        return rows

    @staticmethod
    def _existing_device(rows: List[dict], device: dict) -> Optional[dict]:
        return next((row for row in rows if same_physical_device(row, device)), None)

    @staticmethod
    def _merge_usb_into(existing: dict, usb: dict) -> None:
        """Fold USB detection onto an already-listed (usually runtime) deck."""
        if usb.get("usb_id") and not existing.get("usb_id"):
            existing["usb_id"] = usb["usb_id"]
        if usb.get("usb_path") and not existing.get("usb_path"):
            existing["usb_path"] = usb["usb_path"]
        existing["serial"] = preferred_serial(existing.get("serial"), usb.get("serial"), existing.get("id"), usb.get("id"))

    @staticmethod
    def _settings_for(row: dict, settings_by_id: dict) -> dict:
        for key in (row.get("id"), row.get("serial")):
            if key and key in settings_by_id:
                return settings_by_id[key]
        for device_id, setting in settings_by_id.items():
            if same_physical_device(row, {"id": device_id, "serial": device_id}):
                return setting
        return {}

    # --------------------------------------------------------------- lifecycle
    def install(self, progress: Optional[Callable[[str], None]] = None) -> dict:
        return self.dependencies.ensure(progress)

    def _start_lifecycle_job(self, kind: str, work: Callable, requested_by: str) -> dict:
        try:
            self._acquire_lifecycle("busy")
        except RuntimeError:
            return {"status": "already-running", "job": self.jobs.running(LIFECYCLE_JOBS)}

        def wrapped(handle):
            try:
                return work(handle)
            finally:
                self._lifecycle_lock.release()

        def finished(job: dict) -> None:
            if job["status"] == "failed":
                self._record(last_error=job.get("error"))
                log_event(f"integration-{kind}-failed", error=job.get("error"), requested_by=requested_by)

        try:
            if self.jobs.running(["test-game", "test-script", "test-builtin"]):
                raise RuntimeError("A test is running; wait for it or cancel it before changing tooling.")
            job = self.jobs.start(kind, wrapped, description=f"Stream Deck {kind}", on_finish=finished)
        except Exception:
            self._lifecycle_lock.release()
            raise
        return {"status": "started", "job": job}

    def enable(self, requested_by: str = "") -> dict:
        log_event("integration-enable-requested", requested_by=requested_by)
        return self._start_lifecycle_job("enable", lambda handle: self._enable_work(handle, requested_by), requested_by)

    def _enable_work(self, handle, requested_by: str) -> dict:
        handle.progress("Checking environment...")
        self.paths.ensure_layout()
        self.dependencies.ensure(handle.progress)
        handle.progress("Detecting devices...")
        detected = self.usb_detector()
        handle.progress("Creating configuration...")
        self.config.set_enabled(True)
        self._restart_failures = 0
        handle.progress("Starting Stream Deck service...")
        self.compile_runtime()
        if self.runtime_info()["running"]:
            self.send_command("reload")
        else:
            self.start_runtime()
        handle.progress("Connecting...")
        connected = self._wait_for_runtime(expect_devices=bool(detected))
        message = "Ready." if connected else "Ready. Waiting for a Stream Deck to be connected."
        handle.progress(message)
        log_event("integration-enabled", requested_by=requested_by, devices_connected=connected, usb_detected=len(detected))
        return {"status": "ok", "message": message, "connected_devices": connected}

    def _wait_for_runtime(self, *, expect_devices: bool, timeout: float = CONNECT_WAIT_SECONDS) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self.runtime_info()
            worker = self.worker_status() if info["running"] else {}
            devices = len(worker.get("devices") or [])
            if info["running"] and worker.get("runtime") == "running" and (devices or not expect_devices):
                return devices
            if worker.get("runtime") == "error":
                raise RuntimeError(worker.get("last_error") or "The Stream Deck runtime failed to start.")
            time.sleep(0.25)
        if not self.runtime_info()["running"]:
            raise RuntimeError("The Stream Deck runtime did not start; see Diagnostics -> runtime log.")
        return len(self.worker_status().get("devices") or [])

    def disable(self, requested_by: str = "") -> dict:
        self._acquire_lifecycle("An install or repair is in progress; disable when it finishes.")
        try:
            self.config.set_enabled(False)
            self.stop_runtime()
            log_event("integration-disabled", requested_by=requested_by)
            return {"status": "disabled", "message": "Stream Deck disabled. Profiles, images, and scripts were kept."}
        finally:
            self._lifecycle_lock.release()

    def repair(self, *, reinstall: bool = False, requested_by: str = "") -> dict:
        kind = "reinstall" if reinstall else "repair"
        log_event(f"integration-{kind}-requested", requested_by=requested_by)
        return self._start_lifecycle_job(kind, lambda handle: self._repair_work(handle, reinstall, requested_by), requested_by)

    def _repair_work(self, handle, reinstall: bool, requested_by: str) -> dict:
        if requested_by == "supervisor" and (not self.config.exists() or not self.config.load()["enabled"]):
            return {"status": "ok", "message": "Integration was disabled; automatic repair skipped."}
        handle.progress("Stopping Stream Deck service...")
        self.stop_runtime()
        handle.progress("Checking environment...")
        self.paths.ensure_layout()
        force = reinstall
        if reinstall:
            handle.progress("Removing Stream Deck tooling...")
            self.dependencies.remove()
        elif self.dependencies.installed():
            handle.progress("Verifying installed libraries...")
            try:
                self.dependencies.verify()
            except DependencyError as error:
                handle.progress(f"Libraries are damaged ({error}); reinstalling...")
                force = True
        self.dependencies.ensure(handle.progress, force=force)
        handle.progress("Validating configuration...")
        config = self.config.load()
        self.compile_runtime(config)
        self._restart_failures = 0
        connected = 0
        if config["enabled"]:
            handle.progress("Starting Stream Deck service...")
            self.start_runtime()
            handle.progress("Connecting...")
            connected = self._wait_for_runtime(expect_devices=bool(self.usb_detector()))
        handle.progress("Ready.")
        log_event("integration-repaired", reinstall=reinstall, requested_by=requested_by, devices_connected=connected)
        return {"status": "ok", "reinstalled": reinstall, "connected_devices": connected,
                "config_warnings": self.config.last_warnings}

    def remove(self, *, include_configuration: bool = False, requested_by: str = "") -> dict:
        self._acquire_lifecycle("An install or repair is in progress; try again when it finishes.")
        try:
            if self.jobs.running():
                raise RuntimeError("A test is still running; wait for it or cancel it before removing tooling.")
            if self.config.exists():
                self.config.set_enabled(False)
            self.stop_runtime()
            removed: List[str] = []
            if include_configuration:
                existed = self.paths.remove_root(self.expected_root)
                removed = list(TOOLING_DIRS + CONTENT_DIRS) if existed else []
                self.games.invalidate()
            else:
                if self.config.exists():
                    self.config.set_enabled(False)
                removed.extend(self.dependencies.remove())
                for name in ("rendered", "state"):
                    target = self.paths.owned(name)
                    if target.exists():
                        self.paths.remove_owned_tree(target)
                        removed.append(name)
            log_event("integration-removed", configuration_removed=include_configuration, requested_by=requested_by)
            message = ("Stream Deck tooling and configuration were removed." if include_configuration else
                       "Stream Deck tooling was removed; profiles, scripts, images, and logs were kept.")
            return {"status": "removed", "configuration_removed": include_configuration, "removed": removed,
                    "kept": [] if include_configuration else list(CONTENT_DIRS), "message": message}
        finally:
            self._lifecycle_lock.release()

    # ----------------------------------------------------------- worker process
    def _is_worker_pid(self, pid: int) -> bool:
        cmdline = Path("/proc") / str(pid) / "cmdline"
        if cmdline.parent.parent.is_dir() and Path("/proc/self").exists():
            try:
                text = cmdline.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                return False
            return _WORKER_MODULE in text and str(self.paths.root) in text
        return self._process is not None and self._process.pid == pid

    def _worker_environment(self, app_parent: Path) -> Dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH") or "/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": os.environ.get("HOME") or "/userdata/system",
            "LANG": os.environ.get("LANG") or "C.UTF-8",
            "PYTHONPATH": str(app_parent),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for name in ("DRONE_ES_API_URL", "DRONE_SERVICE_CONTROL_DIR", "DRONE_SERVICE_CONTROL_TIMEOUT_SECONDS", "TZ"):
            if os.environ.get(name):
                env[name] = os.environ[name]
        return env

    def start_runtime(self) -> dict:
        with self._runtime_lock:
            info = self.runtime_info()
            if info["running"]:
                return {"status": "already-running", "pid": info["pid"]}
            if self._process is not None and self._process.poll() is None:
                return {"status": "starting", "pid": self._process.pid}
            if not self.dependencies.installed():
                raise RuntimeError("Stream Deck tooling is not installed; enable or repair the integration first.")
            self.paths.ensure_layout()
            self.paths.worker_status_file.unlink(missing_ok=True)
            app_parent = Path(__file__).resolve().parents[3]
            command = [self.python, "-m", _WORKER_MODULE, "--root", str(self.paths.root),
                       "--roms-root", str(self.settings.roms_root), "--parent-pid", str(os.getpid())]
            with open(self.paths.assert_owned(self.paths.runtime_console_log), "wb") as console:
                process = self.process_factory(
                    command, cwd=str(app_parent), env=self._worker_environment(app_parent),
                    stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT,
                    close_fds=True, start_new_session=True,
                )
            self._process = process
            log_event("runtime-started", pid=process.pid)
            return {"status": "started", "pid": process.pid}

    def stop_runtime(self, timeout: float = 10.0) -> dict:
        with self._runtime_lock:
            info = self.runtime_info()
            if not info["running"]:
                if self._process is not None:
                    # A just-spawned worker may not yet hold its lock. It must
                    # still be stopped, otherwise Disable can leave it alive.
                    self._process.terminate()
                    self._reap(timeout)
                    if self._process is not None:
                        self._process.kill()
                        self._reap(2.0)
                return {"status": "stopped"}
            pid = info["pid"]
            if pid and self._is_worker_pid(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif self._process is not None:
                self._process.terminate()
            deadline = time.monotonic() + timeout
            while lock_is_held(self.paths.runtime_lock) and time.monotonic() < deadline:
                self._reap(0.1)
                time.sleep(0.1)
            if lock_is_held(self.paths.runtime_lock):
                current = self.runtime_info()["pid"]
                if current and current == pid and self._is_worker_pid(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif self._process is not None:
                    self._process.kill()
                self._reap(2.0)
            self._reap(1.0)
            if lock_is_held(self.paths.runtime_lock):
                raise RuntimeError("The runtime did not stop; tooling has been kept intact. Retry Disable.")
            log_event("runtime-stopped", pid=pid)
            return {"status": "stopped"}

    def _reap(self, timeout: float) -> None:
        process = self._process
        if process is None:
            return
        try:
            process.wait(timeout=timeout)
            self._process = None
        except subprocess.TimeoutExpired:
            pass

    def restart_runtime(self) -> dict:
        self.stop_runtime()
        self.compile_runtime()
        return self.start_runtime()

    # --------------------------------------------------------------- supervisor
    def start_supervisor(self) -> bool:
        if self._supervisor is not None and self._supervisor.is_alive():
            return False
        self._supervisor_stop.clear()
        self._supervisor = threading.Thread(target=self._supervise_loop, name="streamdeck-supervisor", daemon=True)
        self._supervisor.start()
        return True

    def _supervise_loop(self) -> None:
        while not self._supervisor_stop.is_set():
            try:
                self.supervise_once()
            except Exception as error:  # noqa: BLE001 - never kill the supervisor
                log_event("supervisor-error", error=str(error))
            self._supervisor_stop.wait(SUPERVISOR_SECONDS)

    def supervise_once(self) -> str:
        """One supervision pass; returns what it did (for tests and logs)."""
        if not self._lifecycle_lock.acquire(blocking=False):
            return "idle"
        try:
            result = self._supervise_locked()
        finally:
            self._lifecycle_lock.release()
        if result == "auto-repair":
            self.repair(requested_by="supervisor")
        return result

    def _supervise_locked(self) -> str:
        if not self.config.exists() or self.jobs.running(LIFECYCLE_JOBS):
            return "idle"
        config = self.config.load()
        if not config["enabled"]:
            return "disabled"
        if not self.dependencies.installed():
            if time.monotonic() - self._last_auto_repair < AUTO_REPAIR_INTERVAL and self._last_auto_repair:
                return "tooling-missing"
            self._last_auto_repair = time.monotonic()
            log_event("supervisor-auto-repair", reason=self.dependencies.status()["reason"])
            return "auto-repair"
        if self.runtime_info()["running"]:
            self._restart_failures = 0
            return "running"
        now = time.monotonic()
        if now < self._next_restart_at:
            return "backoff"
        try:
            self.compile_runtime(config)
            self.start_runtime()
            self._restart_failures += 1
            self._next_restart_at = now + min(RESTART_BACKOFF_MAX, SUPERVISOR_SECONDS * (2 ** self._restart_failures))
            return "started"
        except Exception as error:  # noqa: BLE001
            self._restart_failures += 1
            self._next_restart_at = now + min(RESTART_BACKOFF_MAX, SUPERVISOR_SECONDS * (2 ** self._restart_failures))
            self._record(last_error=f"Runtime start failed: {error}")
            log_event("runtime-start-failed", error=str(error), attempt=self._restart_failures)
            return "failed"

    # ------------------------------------------------------- configuration/apply
    def compile_runtime(self, config: Optional[dict] = None) -> dict:
        config = config or self.config.load()
        self.games.invalidate()
        document = self.compiler.compile(config)
        self.paths.ensure(self.paths.state_dir)
        atomic_write_json(self.paths.runtime_document, document)
        return document

    def profiles_view(self) -> dict:
        config = self.config.load()
        return {"profiles": self.compiler.profile_view(config), "default_profile_id": config["default_profile_id"],
                "context_rules": config["context_rules"], "settings": config["settings"],
                "devices": config["devices"], "warnings": self.config.last_warnings}

    def apply(self, requested_by: str = "") -> dict:
        config = self.config.load()
        document = self.compile_runtime(config)
        problems = [
            {"profile_id": profile["id"], "profile": profile["name"], "key": button["key"], "problem": button["problem"]}
            for profile in document["profiles"] for button in profile["buttons"] if button.get("problem")
        ]
        self.images.prune(image_references(config))
        log_event("configuration-apply", requested_by=requested_by, generation=document["generation"], problems=len(problems))
        if not config["enabled"]:
            return {"status": "saved", "applied": False, "problems": problems,
                    "message": "Saved. Enable Stream Deck to push the configuration to the device."}
        if not self.runtime_info()["running"]:
            return {"status": "saved", "applied": False, "problems": problems,
                    "message": "Saved. The runtime is not running; it will load this configuration when it starts."}
        result = self.send_command("reload", wait=8.0)
        applied = result.get("status") == "ok"
        return {"status": "applied" if applied else "saved", "applied": applied, "problems": problems,
                "devices": result.get("devices"),
                "message": "Applied to the Stream Deck." if applied else "Saved; the runtime did not confirm the update yet."}

    def send_command(self, operation: str, *, device_id: str = "", key: Optional[int] = None,
                     wait: float = 0.0) -> dict:
        if not self.runtime_info()["running"]:
            return {"status": "runtime-stopped", "error": "The Stream Deck runtime is not running."}
        if device_id:
            device_id = safe_id(device_id, "device id")
        command_id = uuid.uuid4().hex
        payload = {"id": command_id, "op": operation, "device_id": device_id, "key": key, "at": _now_iso()}
        self.paths.ensure(self.paths.commands_dir, self.paths.results_dir)
        atomic_write_json(self.paths.commands_dir / f"{time.time_ns()}-{command_id}.json", payload)
        if wait <= 0:
            return {"status": "queued", "id": command_id}
        result_path = self.paths.results_dir / f"{command_id}.json"
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            result = read_json(result_path, None)
            if isinstance(result, dict):
                result_path.unlink(missing_ok=True)
                return result
            time.sleep(0.1)
        return {"status": "timeout", "error": "The Stream Deck runtime did not answer in time."}

    # -------------------------------------------------------------- diagnostics
    def test_connection(self) -> dict:
        usb = self.usb_detector()
        if self.runtime_info()["running"]:
            result = self.send_command("test-connection", wait=8.0)
            result["usb"] = usb
            result["via"] = "runtime"
        elif self.dependencies.installed():
            probe = self.runner.run(self.python, ["-m", _WORKER_MODULE, "--root", str(self.paths.root),
                                                  "--roms-root", str(self.settings.roms_root), "--probe"],
                                    timeout=30, cwd=Path(__file__).resolve().parents[3],
                                    env=self._worker_environment(Path(__file__).resolve().parents[3]))
            try:
                result = json.loads(probe.stdout.strip().splitlines()[-1]) if probe.ok else {
                    "status": "error", "error": probe.summary()}
            except (ValueError, IndexError):
                result = {"status": "error", "error": "The device probe returned no result."}
            result["usb"] = usb
            result["via"] = "probe"
        else:
            result = {"status": "ok" if usb else "no-device", "devices": [], "usb": usb, "via": "usb",
                      "message": "Tooling is not installed yet; only USB detection was performed."}
        self._record(last_test_connection={**result, "at": _now_iso()})
        log_event("test-connection", status=result.get("status"), via=result.get("via"), usb=len(usb))
        return result

    def _verify_image(self, path: Path) -> None:
        if not self.dependencies.installed():
            raise ValueError("Enable Stream Deck first so its isolated image decoder can validate uploads.")
        script = ("import sys; sys.path.insert(0, sys.argv[1]); from PIL import Image; "
                  "Image.MAX_IMAGE_PIXELS = 33554432; im = Image.open(sys.argv[2]); im.verify(); "
                  "im = Image.open(sys.argv[2]); im.load()")
        result = self.runner.run(self.python, ["-c", script, str(self.paths.lib_dir), str(path)], timeout=20,
                                 env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"})
        if not result.ok:
            raise ValueError(result.summary())

    # ------------------------------------------------------------ admin tests
    def start_test_action(self, payload: dict, requested_by: str = "") -> dict:
        self._acquire_lifecycle("Tooling is being changed; wait for it to finish before testing actions.")
        try:
            return self._start_test_action(payload, requested_by)
        finally:
            self._lifecycle_lock.release()

    def _start_test_action(self, payload: dict, requested_by: str = "") -> dict:
        payload = payload if isinstance(payload, dict) else {}
        action_type = str(payload.get("action_type") or "")
        confirmed = payload.get("confirmed") is True
        if action_type == "builtin":
            action_id = str(payload.get("action_id") or "")
            try:
                action = self.actions.resolve(action_id)
            except UnknownActionError as error:
                raise KeyError(str(error.args[0])) from error
            if action.confirmation_required and not confirmed:
                raise ValueError("this system action must be confirmed before testing")
            button = {"action_type": "builtin", "action_id": action.id}
        elif action_type == "game":
            game = validate_game(payload.get("game"))
            resolved = self.games.resolve(game)
            if not resolved["installed"]:
                raise ValueError(resolved.get("reason") or "Game not found. Relink this button.")
            if not confirmed:
                raise ValueError("testing a game launch may close the running game and must be confirmed")
            button = {"action_type": "game", "game": resolved}
        elif action_type == "script":
            button = {"action_type": "script", "script_id": self.scripts.get(str(payload.get("script_id") or ""),
                                                                             include_code=False)["id"]}
        else:
            raise ValueError("only built-in actions, Launch Game, and custom scripts can be tested from the browser")

        settings = self.config.load()["settings"]
        self.coordinator.exit_timeout = float(settings["exit_timeout_seconds"])
        self.coordinator.start_timeout = float(settings["launch_confirm_timeout_seconds"])

        def work(handle) -> dict:
            context = ActionContext.for_game(self.game_runtime.get_active_game(), trigger="test",
                                             confirmed=confirmed or action_type == "script", requested_by=requested_by)
            if action_type == "script":
                result = self.scripts.run(button["script_id"], timeout=float(settings["script_timeout_seconds"]),
                                          context={"DRONE_STREAMDECK_TRIGGER": "test"},
                                          cancel_event=handle.cancel_event, requested_by=requested_by)
            else:
                result = self.dispatcher.execute(button, context)
            event = {"action_type": action_type, "trigger": "test", "at": _now_iso(),
                     "action_id": button.get("action_id") or button.get("script_id") or (button.get("game") or {}).get("id")}
            summary = {key: value for key, value in result.items() if key not in ("stdout", "stderr")}
            self._record(last_action=event, last_action_result={**summary, "at": event["at"]})
            if action_type == "game":
                self._record(last_game_launch={**summary, "at": event["at"], "game": button["game"].get("name")})
            if result.get("status") in ("error", "failed", "timed-out"):
                self._record(last_error=str(result.get("error") or result.get("status")))
            return result

        return self.jobs.start(f"test-{action_type}", work, description=f"Test {action_type}",
                               cancellable=action_type == "script")

    def script_references(self, script_id: str) -> List[dict]:
        return script_references(self.config.load(), script_id)

    # ------------------------------------------------------ validated edits
    def set_button(self, profile_id: str, key: Any, payload: dict, requested_by: str = "") -> dict:
        """Assign a key after checking every reference against what exists locally."""
        payload = dict(payload if isinstance(payload, dict) else {})
        action_type = str(payload.get("action_type") or payload.get("actionType") or "none")
        if action_type == "builtin":
            try:
                self.actions.resolve(str(payload.get("action_id") or payload.get("actionId") or ""))
            except UnknownActionError as error:
                raise ValueError(str(error.args[0])) from error
        elif action_type == "script":
            try:
                self.scripts.get(str(payload.get("script_id") or payload.get("scriptId") or ""), include_code=False)
            except KeyError as error:
                raise ValueError("the selected script does not exist") from error
        elif action_type == "game":
            resolved = self.games.resolve(validate_game(payload.get("game")))
            if not resolved["installed"]:
                raise ValueError(resolved.get("reason") or "Game not found in the local library.")
            payload["game"] = {key_: resolved[key_] for key_ in ("id", "name", "system", "rom_path")}
            payload["game"]["metadata_source"] = "drone-rom-cache"
        image = payload.get("image") if isinstance(payload.get("image"), dict) else {}
        if image.get("type") == "uploaded":
            try:
                self.images.get(str(image.get("image_id") or image.get("imageId") or ""))
            except KeyError as error:
                raise ValueError("the uploaded image no longer exists; upload it again") from error
        try:
            key_index = int(key)
        except (TypeError, ValueError) as error:
            raise ValueError("button key must be an integer") from error
        button = self.config.set_button(profile_id, key_index, payload)
        log_event("button-configured", profile_id=profile_id, key=key_index, action_type=button["action_type"],
                  action_id=button.get("action_id"), game_id=(button.get("game") or {}).get("id"),
                  script_id=button.get("script_id"), requested_by=requested_by)
        applied = self.apply(requested_by) if self.config.load()["settings"]["auto_apply"] else None
        return {"button": button, "applied": applied}

    def set_device_settings(self, device_id: str, payload: dict, requested_by: str = "") -> dict:
        row = self.config.set_device_settings(device_id, payload if isinstance(payload, dict) else {})
        log_event("device-settings", device_id=row["device_id"], brightness=row["brightness"],
                  startup_profile_id=row["startup_profile_id"], requested_by=requested_by)
        applied = None
        if self.config.load()["enabled"] and self.runtime_info()["running"]:
            self.compile_runtime()
            applied = self.send_command("reload", wait=5.0)
        return {"device": row, "applied": applied}

    LOG_SOURCES = ("runtime", "install", "console")

    def log_tail(self, source: str, lines: int = 200) -> dict:
        try:
            from ...common.logtail import _tail_lines
        except ImportError:  # pragma: no cover - flat execution
            from common.logtail import _tail_lines  # type: ignore
        paths = {"runtime": self.paths.runtime_log, "install": self.paths.install_log,
                 "console": self.paths.runtime_console_log}
        if source not in paths:
            raise ValueError("unknown log source")
        path = self.paths.assert_owned(paths[source])
        lines = min(2000, max(1, int(lines)))
        content = _tail_lines(path, lines, max_bytes=512 * 1024) if path.is_file() else []
        return {"source": source, "path": str(path), "lines": content}


_MANAGERS: Dict[tuple, StreamDeckIntegration] = {}
_MANAGERS_LOCK = threading.Lock()


def get_streamdeck_integration(settings: Any, repository: Any) -> StreamDeckIntegration:
    """The process-wide manager for this install root (one supervisor, one worker)."""
    key = (str(StreamDeckPaths.default().root), os.path.abspath(str(settings.roms_root)))
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = StreamDeckIntegration(settings, repository)
            _MANAGERS[key] = manager
        elif manager.repository is not repository:
            manager.settings, manager.repository = settings, repository
            manager.games.repository = repository
        return manager
