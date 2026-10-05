"""The hardware runtime core that runs inside the isolated worker process.

Depends only on abstractions (``DeviceProvider``/``StreamDeckDevice``, a key
renderer, ``ButtonDispatcher``, ``GameRuntime``) so it is exercised in tests
with fake hardware and no third-party libraries.

Input rules: key-down is the trigger (key-up never triggers a second run);
repeated key-downs inside ``DEBOUNCE_SECONDS`` are ignored; a key whose
previous action is still running ignores new presses; dangerous built-ins
(reboot/shutdown/restart ES) require holding the key for the configured hold
duration -- releasing early cancels. Hotplug: attached/removed decks are
reconciled on a cheap USB-signature change (sysfs) or periodically, failures
are retried with backoff, and each distinct error is logged once.
"""

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .actions import ActionContext, BuiltInActionRegistry, UnknownActionError
from .config import DEFAULT_BRIGHTNESS
from .devices import DeviceProvider, StreamDeckDevice, usb_signature
from .dispatcher import ButtonDispatcher
from .game_runtime import GameRuntime
from .logs import log_event
from .paths import StreamDeckPaths, atomic_write_json, read_json


DEBOUNCE_SECONDS = 0.25
RECONCILE_SECONDS = 3.0
USB_PROBE_SECONDS = 1.0
MAX_STATUS_ERRORS = 10
FULL_ENUMERATE_SECONDS = 30.0
ATTACH_RETRY_SECONDS = 10.0
CONTEXT_POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 60.0
IDENTIFY_SECONDS = 4.0
TEST_FLASH_SECONDS = 1.5
COMMAND_OPERATIONS = {"reload", "identify", "test-button", "test-connection"}
BLANK = {"kind": "blank"}
ERROR_SPEC = {"kind": "generated", "text": "ERR", "background": "#7f1d1d", "text_color": "#ffffff"}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _start_thread(target: Callable[[], None], name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


class DeviceSlot:
    def __init__(self, device: StreamDeckDevice, device_id: str, capabilities: dict) -> None:
        self.device = device
        self.device_id = device_id
        self.capabilities = capabilities
        self.active_profile_id = ""
        self.attached_at = _now_iso()
        self.down: Dict[int, float] = {}
        self.tokens: Dict[int, int] = {}
        self.last_trigger: Dict[int, float] = {}
        self.busy: set = set()
        self.hold_timers: Dict[int, Any] = {}
        self.rendered: Dict[int, str] = {}
        self.io_lock = threading.RLock()


class StreamDeckRuntime:
    def __init__(
        self,
        paths: StreamDeckPaths,
        provider: DeviceProvider,
        renderer: Any,
        dispatcher: ButtonDispatcher,
        actions: BuiltInActionRegistry,
        game_runtime: GameRuntime,
        *,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None], str], None] = _start_thread,
        timer_factory: Callable[..., Any] = threading.Timer,
        usb_probe: Callable[[], Optional[str]] = usb_signature,
        status_writer: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.paths = paths
        self.provider = provider
        self.renderer = renderer
        self.dispatcher = dispatcher
        self.actions = actions
        self.game_runtime = game_runtime
        self.clock = clock
        self.spawn = spawn
        self.timer_factory = timer_factory
        self.usb_probe = usb_probe
        self.status_writer = status_writer or (lambda status: atomic_write_json(paths.worker_status_file, status))
        self.document: dict = {"generation": 0, "profiles": [], "devices": [], "settings": {}, "context_rules": []}
        self.slots: Dict[str, DeviceSlot] = {}
        self.known_device_ids: set = set()
        self._lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._next_reconcile = 0.0
        self._next_full_enumerate = 0.0
        self._next_context_poll = 0.0
        self._last_usb_signature: Optional[str] = None
        self._next_usb_probe = 0.0
        self._next_connected_check = 0.0
        self._last_apply_failed: List[str] = []
        self._attach_failures: Dict[str, float] = {}
        self._logged_errors: Dict[str, str] = {}
        self._last_written: Optional[dict] = None
        self._last_heartbeat = 0.0
        self.status: Dict[str, Any] = {
            "runtime": "running", "pid": os.getpid(), "started_at": _now_iso(), "devices": [],
            "applied_generation": 0, "last_error": "", "errors": [], "last_button_press": None, "last_action": None,
            "last_action_result": None, "last_game_launch": None, "last_device_connection": None,
        }
        game_runtime.subscribe(self._on_game_event)

    # -- helpers -----------------------------------------------------------------------
    def _log_error_once(self, scope: str, message: str) -> None:
        """Record an active problem; each distinct (scope, message) is logged once."""
        if self._logged_errors.get(scope) != message:
            self._logged_errors.pop(scope, None)
            self._logged_errors[scope] = message
            log_event("runtime-error", scope=scope, error=message)
        self.status["last_error"] = message
        self.status["errors"] = list(self._logged_errors.values())[-MAX_STATUS_ERRORS:]

    def _clear_errors(self, *scopes: str, prefix: str = "") -> None:
        """Forget problems that are resolved (one device's success never clears another's)."""
        stale = [scope for scope in self._logged_errors if scope in scopes or (prefix and scope.startswith(prefix))]
        if not stale:
            return
        cleared = {self._logged_errors.pop(scope) for scope in stale}
        remaining = list(self._logged_errors.values())
        self.status["errors"] = remaining[-MAX_STATUS_ERRORS:]
        if self.status.get("last_error") in cleared:
            self.status["last_error"] = remaining[-1] if remaining else ""

    def _settings(self) -> dict:
        return self.document.get("settings") or {}

    def _profile(self, profile_id: str) -> dict:
        profiles = self.document.get("profiles") or []
        for profile in profiles:
            if profile["id"] == profile_id:
                return profile
        default_id = self.document.get("default_profile_id")
        for profile in profiles:
            if profile["id"] == default_id:
                return profile
        return profiles[0] if profiles else {"id": "default", "name": "Default", "buttons": []}

    def _device_settings(self, device_id: str) -> dict:
        return next((row for row in self.document.get("devices") or [] if row.get("device_id") == device_id), {})

    def _button(self, slot: DeviceSlot, key: int) -> Optional[dict]:
        profile = self._profile(slot.active_profile_id)
        return next((row for row in profile.get("buttons") or [] if int(row.get("key", -1)) == key), None)

    def _needs_hold(self, button: dict) -> bool:
        if button.get("action_type") != "builtin" or not self._settings().get("confirm_dangerous_actions", True):
            return False
        try:
            return self.actions.resolve(button.get("action_id", "")).confirmation_required
        except UnknownActionError:
            return False

    # -- status ----------------------------------------------------------------------
    def publish(self, *, force: bool = False) -> None:
        with self._status_lock:
            with self._lock:
                self.status["devices"] = [
                    {**slot.capabilities, "id": slot.device_id, "active_profile_id": self._profile(slot.active_profile_id)["id"],
                     "attached_at": slot.attached_at, "runtime_state": "open"}
                    for slot in self.slots.values()
                ]
            comparable = {key: value for key, value in self.status.items() if key != "heartbeat_at"}
            now = self.clock()
            if not force and comparable == self._last_written and now - self._last_heartbeat < HEARTBEAT_SECONDS:
                return
            self._last_written = comparable
            self._last_heartbeat = now
            self.status["heartbeat_at"] = _now_iso()
            try:
                self.status_writer(dict(self.status))
            except OSError as error:
                self._log_error_once("status", f"Could not write runtime status: {error}")

    # -- configuration -------------------------------------------------------------------
    def load_document(self, *, force: bool = False) -> bool:
        document = read_json(self.paths.runtime_document, None)
        if not isinstance(document, dict) or not isinstance(document.get("profiles"), list):
            return False
        if not force and document.get("generation") == self.document.get("generation"):
            return False
        with self._lock:
            for slot in self.slots.values():
                self._cancel_holds(slot)
            self.document = document
            settings = document.get("settings") or {}
            coordinator = self.dispatcher.coordinator
            coordinator.exit_timeout = float(settings.get("exit_timeout_seconds", coordinator.exit_timeout))
            coordinator.start_timeout = float(settings.get("launch_confirm_timeout_seconds", coordinator.start_timeout))
            profile_ids = {profile["id"] for profile in document["profiles"]}
            slots = list(self.slots.values())
            for slot in slots:
                if slot.active_profile_id not in profile_ids:
                    slot.active_profile_id = self._startup_profile(slot.device_id)
        # A deck that fails here is detached by apply_device and re-attached
        # (with this document) by the next reconcile; the others keep working.
        self._last_apply_failed = [slot.device_id for slot in slots if not self.apply_device(slot)]
        self.status["applied_generation"] = document.get("generation")
        self.status["applied_at"] = _now_iso()
        log_event("configuration-applied", generation=document.get("generation"), devices=len(slots),
                  failed=len(self._last_apply_failed) or None)
        self.publish()
        return True

    def _startup_profile(self, device_id: str) -> str:
        configured = self._device_settings(device_id).get("startup_profile_id")
        return self._profile(configured or self.document.get("default_profile_id") or "")["id"]

    # -- devices ------------------------------------------------------------------------------
    def reconcile(self, *, force: bool = False) -> None:
        now = self.clock()
        with self._lock:
            slots = list(self.slots.items())
        # The sysfs USB fingerprint is the cheap hotplug signal; sample it at
        # most once a second rather than on every loop iteration.
        signature = self._last_usb_signature
        if force or now >= self._next_usb_probe:
            self._next_usb_probe = now + USB_PROBE_SECONDS
            signature = self.usb_probe()
        changed = signature is not None and signature != self._last_usb_signature
        # ``connected()`` re-enumerates HID devices in the library, so only ask
        # when USB changed or every few seconds (write errors detach at once).
        if force or changed or now >= self._next_connected_check:
            self._next_connected_check = now + RECONCILE_SECONDS
            for transport_id, slot in slots:
                if not slot.device.connected():
                    self.detach(transport_id, reason="disconnected")
        due = force or changed or now >= self._next_full_enumerate or (signature is None and now >= self._next_reconcile)
        if not due:
            return
        self._last_usb_signature = signature
        self._next_reconcile = now + RECONCILE_SECONDS
        self._next_full_enumerate = now + FULL_ENUMERATE_SECONDS
        try:
            devices = self.provider.enumerate()
        except Exception as error:  # noqa: BLE001 - e.g. HID backend missing
            self._log_error_once("enumerate", f"Device discovery failed: {error}")
            self.publish()
            return
        self._clear_errors("enumerate")
        present = {device.transport_id: device for device in devices}
        for transport_id in [tid for tid, _slot in slots if tid not in present]:
            self.detach(transport_id, reason="unplugged")
        for transport_id, device in present.items():
            with self._lock:
                attached = transport_id in self.slots
            failed_at = self._attach_failures.get(transport_id)
            if attached or (failed_at is not None and now - failed_at < ATTACH_RETRY_SECONDS and not force):
                continue
            self.attach(device)
        self.publish()

    def attach(self, device: StreamDeckDevice) -> Optional[DeviceSlot]:
        transport_id = device.transport_id
        try:
            device.open()
            device.reset()
            capabilities = device.get_capabilities()
            device_id = capabilities["id"]
            slot = DeviceSlot(device, device_id, capabilities)
            slot.active_profile_id = self._startup_profile(device_id)
            device.register_key_callback(self.on_key)
            with self._lock:
                self.slots[transport_id] = slot
            if not self.apply_device(slot):
                raise OSError("initial device communication failed")
        except Exception as error:  # noqa: BLE001 - keep running with other decks
            self._attach_failures[transport_id] = self.clock()
            try:
                device.close()
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                self.slots.pop(transport_id, None)
            self._log_error_once(f"attach:{transport_id}", f"Could not open {device.model}: {error}")
            return None
        self._attach_failures.pop(transport_id, None)
        self._clear_errors(f"attach:{transport_id}", f"device:{device_id}")
        event = "device-reconnect" if device_id in self.known_device_ids else "device-attach"
        self.known_device_ids.add(device_id)
        log_event(event, device_id=device_id, model=capabilities["model"], keys=capabilities["key_count"],
                  layout=f"{capabilities['rows']}x{capabilities['columns']}")
        self.status["last_device_connection"] = {"device_id": device_id, "model": capabilities["model"], "at": _now_iso()}
        return slot

    def detach(self, transport_id: str, *, reason: str) -> None:
        with self._lock:
            slot = self.slots.pop(transport_id, None)
        if slot is None:
            return
        self._cancel_holds(slot)
        try:
            slot.device.register_key_callback(None)
        except Exception:  # noqa: BLE001
            pass
        try:
            slot.device.close()
        except Exception:  # noqa: BLE001
            pass
        self._clear_errors(prefix=f"render:{slot.device_id}:")
        log_event("device-detach", device_id=slot.device_id, reason=reason)

    def _slot_for(self, device: StreamDeckDevice) -> Optional[DeviceSlot]:
        with self._lock:
            return self.slots.get(device.transport_id)

    def _cancel_holds(self, slot: DeviceSlot) -> None:
        # A held key must never execute an action from a newly applied profile.
        for timer in list(slot.hold_timers.values()):
            timer.cancel()
        slot.hold_timers.clear()

    def _push(self, slot: DeviceSlot, key: int, spec: dict, *, remember: bool = True) -> None:
        """Render ``spec`` and write it to one key. Device I/O errors propagate."""
        size = slot.device.key_image_size
        scope = f"render:{slot.device_id}:{key}"
        try:
            image = self.renderer.render(spec, size)
            if remember:
                self._clear_errors(scope)
        except Exception as error:  # noqa: BLE001 - one bad image must not blank the deck
            self._log_error_once(scope, f"Key {key + 1} image could not be rendered: {error}")
            image = self.renderer.render(ERROR_SPEC, size)
            remember = False
        slot.device.set_key_image(key, slot.device.to_native(image))
        if remember:
            marker = repr(sorted(spec.items()))
            if slot.rendered.get(key) != marker:
                slot.rendered[key] = marker
                write_preview = getattr(self.renderer, "write_preview", None)
                if write_preview is not None:
                    write_preview(image, self.paths.preview_dir / slot.device_id / f"{key}.png")

    def apply_device(self, slot: DeviceSlot) -> bool:
        try:
            with slot.io_lock:
                brightness = self._device_settings(slot.device_id).get("brightness")
                slot.device.set_brightness(int(DEFAULT_BRIGHTNESS if brightness is None else brightness))
                if not slot.device.has_key_images:
                    return True
                profile = self._profile(slot.active_profile_id)
                buttons = {int(row.get("key", -1)): row for row in profile.get("buttons") or []}
                for key in range(slot.device.key_count):
                    self._push(slot, key, (buttons.get(key) or {}).get("render") or BLANK)
            return True
        except Exception as error:  # noqa: BLE001 - device I/O failure -> detach, reconcile reattaches
            self._log_error_once(f"device:{slot.device_id}", f"Communication with {slot.capabilities.get('model')} failed: {error}")
            self.detach(slot.device.transport_id, reason="communication error")
            return False

    def restore_key(self, slot: DeviceSlot, key: int) -> None:
        button = self._button(slot, key)
        try:
            with slot.io_lock:
                self._push(slot, key, (button or {}).get("render") or BLANK)
        except Exception as error:  # noqa: BLE001
            self._log_error_once(f"device:{slot.device_id}", f"Communication failed: {error}")

    # -- input -------------------------------------------------------------------------------
    def on_key(self, device: StreamDeckDevice, key: int, pressed: bool) -> None:
        slot = self._slot_for(device)
        if slot is None:
            return
        key = int(key)
        if not 0 <= key < slot.device.key_count:
            return
        now = self.clock()
        with self._lock:
            if pressed:
                if key in slot.down:
                    return  # duplicate key-down without a key-up
                slot.down[key] = now
                token = slot.tokens.get(key, 0) + 1
                slot.tokens[key] = token
                if now - slot.last_trigger.get(key, -1e9) < DEBOUNCE_SECONDS:
                    return
                button = self._button(slot, key)
            else:
                slot.down.pop(key, None)
                timer = slot.hold_timers.pop(key, None)
                if timer is None:
                    return
                timer.cancel()
        if not pressed:
            self.restore_key(slot, key)
            log_event("dangerous-action-cancelled", device_id=slot.device_id, key=key, reason="released before hold time")
            self.status["last_action_result"] = {"status": "cancelled", "key": key, "at": _now_iso(),
                                                 "error": "Released before the hold time; the action did not run."}
            self.publish()
            return
        self.status["last_button_press"] = {"device_id": slot.device_id, "key": key, "at": _now_iso(),
                                            "profile_id": slot.active_profile_id}
        log_event("button-press", device_id=slot.device_id, key=key, profile_id=slot.active_profile_id,
                  action_type=(button or {}).get("action_type", "none"))
        if not button or button.get("action_type", "none") == "none":
            self.publish()
            return
        if self._needs_hold(button):
            hold_ms = int(self._settings().get("hold_duration_ms", 1500))
            timer = self.timer_factory(hold_ms / 1000.0, self._hold_elapsed, args=(slot, key, token))
            timer.daemon = True
            with self._lock:
                slot.hold_timers[key] = timer
            try:
                with slot.io_lock:
                    self._push(slot, key, {"kind": "generated", "text": "HOLD", "secondary_text": "to confirm",
                                           "background": "#b45309", "text_color": "#ffffff"}, remember=False)
            except Exception:  # noqa: BLE001 - feedback only
                pass
            timer.start()
            log_event("dangerous-action-hold-started", device_id=slot.device_id, key=key, hold_ms=hold_ms)
            self.publish()
            return
        # No hold needed: either not a dangerous action, or the administrator turned
        # off "confirm dangerous actions" -- the press itself is the confirmation.
        self._trigger(slot, key, button, confirmed=True)

    def _hold_elapsed(self, slot: DeviceSlot, key: int, token: int) -> None:
        with self._lock:
            if key not in slot.down or slot.tokens.get(key) != token or slot.hold_timers.pop(key, None) is None:
                return
            button = self._button(slot, key)
        self.restore_key(slot, key)
        if button:
            self._trigger(slot, key, button, confirmed=True)

    def _trigger(self, slot: DeviceSlot, key: int, button: dict, *, confirmed: bool) -> None:
        with self._lock:
            if key in slot.busy:
                log_event("button-ignored", device_id=slot.device_id, key=key, reason="previous action still running")
                return
            slot.busy.add(key)
            slot.last_trigger[key] = self.clock()
            profile_id = slot.active_profile_id

        def run() -> None:
            started = time.monotonic()
            context = ActionContext.for_game(
                self.game_runtime.get_active_game(), device_id=slot.device_id, profile_id=profile_id,
                key_index=key, trigger="button", confirmed=confirmed,
            )
            try:
                result = self.dispatcher.execute(button, context, navigate=lambda target: self.navigate(slot, target))
            except Exception as error:  # noqa: BLE001
                result = {"status": "error", "error": str(error)}
            finally:
                with self._lock:
                    slot.busy.discard(key)
            action = {"device_id": slot.device_id, "key": key, "action_type": button.get("action_type"),
                      "action_id": button.get("action_id") or button.get("script_id") or (button.get("game") or {}).get("id"),
                      "at": _now_iso(), "duration_seconds": round(time.monotonic() - started, 3)}
            self.status["last_action"] = action
            self.status["last_action_result"] = {**{key_: value for key_, value in result.items()
                                                    if key_ not in ("stdout", "stderr")}, "at": action["at"]}
            if button.get("action_type") == "game":
                self.status["last_game_launch"] = {**self.status["last_action_result"], "game": (button.get("game") or {}).get("name")}
            if result.get("status") in ("error", "failed", "timed-out"):
                self.status["last_error"] = str(result.get("error") or result.get("status"))
            self.publish()

        self.spawn(run, f"streamdeck-key-{key}")

    def navigate(self, slot: DeviceSlot, button: dict) -> dict:
        profiles = self.document.get("profiles") or []
        if not profiles:
            return {"status": "error", "error": "No profiles are configured."}
        operation = button.get("operation")
        if operation == "go-to":
            target = next((row for row in profiles if row["id"] == button.get("profile_id")), None)
            if target is None:
                return {"status": "error", "error": "The target profile no longer exists."}
        else:
            current = self._profile(slot.active_profile_id)
            index = next((i for i, row in enumerate(profiles) if row["id"] == current["id"]), 0)
            target = profiles[(index + (1 if operation == "next" else -1)) % len(profiles)]
        self.switch_profile(slot, target["id"], reason=f"button:{operation}")
        return {"status": "ok", "profile_id": target["id"], "profile_name": target.get("name")}

    def switch_profile(self, slot: DeviceSlot, profile_id: str, *, reason: str) -> None:
        with self._lock:
            if slot.active_profile_id == profile_id:
                return
            self._cancel_holds(slot)
            slot.active_profile_id = profile_id
        log_event("profile-change", device_id=slot.device_id, profile_id=profile_id, reason=reason)
        self.apply_device(slot)
        self.publish()

    # -- context rules --------------------------------------------------------------------------
    def _on_game_event(self, event: str, game: dict) -> None:
        for rule in self.document.get("context_rules") or []:
            if not rule.get("enabled", True) or rule.get("event") != event:
                continue
            if rule.get("system") and rule["system"].lower() != str(game.get("system") or "").lower():
                continue
            emulator = rule.get("emulator")
            if emulator and emulator.lower() not in {str(game.get("emulator") or "").lower(), str(game.get("core") or "").lower()}:
                continue
            target = rule.get("profile_id")
            if not any(profile["id"] == target for profile in self.document.get("profiles") or []):
                continue
            with self._lock:
                slots = list(self.slots.values())
            for slot in slots:
                self.switch_profile(slot, target, reason=f"rule:{rule.get('id')}:{event}")
            return

    # -- commands from the Drone -----------------------------------------------------------------
    def process_commands(self) -> None:
        try:
            files = sorted(self.paths.commands_dir.glob("*.json"))
        except OSError:
            return
        for path in files[:20]:
            command = read_json(path, None)
            path.unlink(missing_ok=True)
            if not isinstance(command, dict) or command.get("op") not in COMMAND_OPERATIONS:
                continue
            try:
                result = self.handle_command(command)
            except Exception as error:  # noqa: BLE001
                result = {"status": "error", "error": str(error)}
            command_id = str(command.get("id") or "")
            if command_id.isalnum() and len(command_id) <= 64:
                atomic_write_json(self.paths.results_dir / f"{command_id}.json", {**result, "op": command["op"], "at": _now_iso()})

    def _targets(self, device_id: str) -> List[DeviceSlot]:
        with self._lock:
            slots = list(self.slots.values())
        return [slot for slot in slots if not device_id or slot.device_id == device_id]

    def handle_command(self, command: dict) -> dict:
        operation = command["op"]
        targets = self._targets(str(command.get("device_id") or ""))
        if operation == "reload":
            if not self.load_document(force=True):
                return {"status": "error", "error": "The runtime configuration is invalid; repair and apply again."}
            if self._last_apply_failed:
                return {"status": "error", "devices": len(self.slots), "failed_devices": list(self._last_apply_failed),
                        "error": "Some Stream Decks could not be updated; they reconnect automatically and "
                                 "receive this configuration then. See the runtime log."}
            return {"status": "ok", "devices": len(self.slots)}
        if not targets:
            return {"status": "no-device", "error": "No matching Stream Deck is connected."}
        if operation == "identify":
            for slot in targets:
                with slot.io_lock:
                    for key in range(slot.device.key_count):
                        if slot.device.has_key_images:
                            self._push(slot, key, {"kind": "generated", "text": str(key + 1), "background": "#2563eb",
                                                   "text_color": "#ffffff", "text_size": "large"}, remember=False)
                timer = self.timer_factory(IDENTIFY_SECONDS, self.apply_device, args=(slot,))
                timer.daemon = True
                timer.start()
            log_event("identify-buttons", devices=len(targets))
            return {"status": "ok", "devices": [slot.device_id for slot in targets], "seconds": IDENTIFY_SECONDS}
        if operation == "test-button":
            key = int(command.get("key", -1))
            slot = targets[0]
            if not 0 <= key < slot.device.key_count:
                return {"status": "error", "error": "That key does not exist on this device."}
            with slot.io_lock:
                self._push(slot, key, {"kind": "generated", "text": "TEST", "secondary_text": f"key {key + 1}",
                                       "background": "#16a34a", "text_color": "#ffffff"}, remember=False)
            timer = self.timer_factory(TEST_FLASH_SECONDS, self.restore_key, args=(slot, key))
            timer.daemon = True
            timer.start()
            log_event("test-button", device_id=slot.device_id, key=key)
            return {"status": "ok", "device_id": slot.device_id, "key": key, "action_executed": False}
        if operation == "test-connection":
            report = []
            for slot in targets:
                entry = {"device_id": slot.device_id, "model": slot.capabilities.get("model")}
                try:
                    entry["capabilities"] = slot.device.get_capabilities()
                    with slot.io_lock:
                        brightness = self._device_settings(slot.device_id).get("brightness")
                        slot.device.set_brightness(int(DEFAULT_BRIGHTNESS if brightness is None else brightness))
                    entry["communication"] = "ok"
                except Exception as error:  # noqa: BLE001
                    entry["communication"] = f"failed: {error}"
                report.append(entry)
            ok = all(entry["communication"] == "ok" for entry in report)
            return {"status": "ok" if ok else "error", "devices": report}
        return {"status": "error", "error": "unsupported command"}

    # -- loop ---------------------------------------------------------------------------------
    def tick(self) -> None:
        self.load_document()
        self.reconcile()
        self.process_commands()
        now = self.clock()
        if now >= self._next_context_poll:
            self._next_context_poll = now + CONTEXT_POLL_SECONDS
            self.game_runtime.poll()
        self.status["launch_state"] = self.dispatcher.coordinator.state if hasattr(self.dispatcher.coordinator, "state") else "IDLE"
        self.publish()

    def shutdown(self) -> None:
        with self._lock:
            transport_ids = list(self.slots)
        for transport_id in transport_ids:
            slot = self.slots.get(transport_id)
            if slot is not None:
                try:
                    with slot.io_lock:
                        slot.device.reset()
                except Exception:  # noqa: BLE001
                    pass
            self.detach(transport_id, reason="runtime stopping")
        self.status.update(runtime="stopped", devices=[])
        self.publish(force=True)
