"""Patches gaps in the vendored SDL2Renderer's gamepad support -- plain
functions called from ui/app.py's run()/_run_loop, mirroring how
_fix_hidpi_framebuffer_scale already patches a separate vendored-backend
bug there rather than subclassing SDL2Renderer.

Three real gaps, found reading imgui_bundle.python_backends.sdl2_backend
directly (no vendored backend in the package -- sdl2, sdl3, glfw, pygame,
pyglet -- covers any of these):

1. It never sets imgui.BackendFlags_.has_gamepad, which ImGui's nav system
   checks before gamepad navigation activates at all -- so even though
   D-pad/face-button events were already being fed in, nav wasn't actually
   live.
2. Zero analog left-stick support. ImGui's io.add_key_analog_event expects
   the *current* held value every frame (it drives nav-repeat timing, same
   as a held D-pad button); SDL_CONTROLLERAXISMOTION only fires on change,
   so this has to be a per-frame poll via SDL_GameControllerGetAxis, not
   event-driven.
3. Shoulder buttons (L1/R1) aren't translated to any imgui.Key at all --
   only used here for the optional quick-tab-switch bonus in ui/shell.py.
"""

import ctypes

import sdl2
from imgui_bundle import imgui

# A common, defensible default -- not a validated value. Worn analog sticks
# or different controllers may need something in the 0.15-0.35 range;
# revisit after real-hardware testing (see ports-client/README.md).
_DEADZONE = 0.25

_AXIS_MAX = 32767.0

# Keyed by SDL's *joystick instance ID* (stable across a controller's
# lifetime), not the device index SDL_CONTROLLERDEVICEADDED hands you (which
# shifts as controllers come and go) -- see handle_device_added/_removed.
_open_controllers: dict = {}


def _apply_deadzone(raw_axis_value: int, deadzone: float = _DEADZONE) -> float:
    """Normalize an int16 SDL axis reading (-32768..32767) to -1.0..1.0,
    rescaling past the deadzone so the value doesn't jump discontinuously
    from 0.0 to `deadzone` the instant the stick crosses the threshold.
    Pure function, no SDL/imgui dependency -- unit-testable in isolation.
    """
    normalized = max(-1.0, min(1.0, raw_axis_value / _AXIS_MAX))
    magnitude = abs(normalized)
    if magnitude <= deadzone:
        return 0.0
    sign = 1.0 if normalized > 0 else -1.0
    return sign * (magnitude - deadzone) / (1.0 - deadzone)


def _sync_has_gamepad_flag(io) -> None:
    if _open_controllers:
        io.backend_flags |= imgui.BackendFlags_.has_gamepad.value
    else:
        io.backend_flags &= ~imgui.BackendFlags_.has_gamepad.value


def open_all_connected(io) -> None:
    """Opens every already-connected controller at startup and syncs the
    HasGamepad flag once. Same SDL_NumJoysticks()/SDL_IsGameController()
    loop PortsClientApp._open_connected_game_controllers used to run
    directly; this additionally tracks each handle's instance ID so a
    later SDL_CONTROLLERDEVICEREMOVED can close the right one.
    """
    for index in range(sdl2.SDL_NumJoysticks()):
        if not sdl2.SDL_IsGameController(index):
            continue
        handle = sdl2.SDL_GameControllerOpen(index)
        if not handle:
            continue
        joystick = sdl2.SDL_GameControllerGetJoystick(handle)
        instance_id = sdl2.SDL_JoystickInstanceID(joystick)
        _open_controllers[instance_id] = handle
    _sync_has_gamepad_flag(io)


def handle_device_added(event, io) -> None:
    """SDL_CONTROLLERDEVICEADDED's event.cdevice.which is a *device index*
    (positional, shifts as controllers come and go) -- resolve and store
    the real instance ID at open time so a later removal can find it.
    """
    handle = sdl2.SDL_GameControllerOpen(event.cdevice.which)
    if not handle:
        return
    joystick = sdl2.SDL_GameControllerGetJoystick(handle)
    instance_id = sdl2.SDL_JoystickInstanceID(joystick)
    _open_controllers[instance_id] = handle
    _sync_has_gamepad_flag(io)


def handle_device_removed(event, io) -> None:
    """Unlike ADDED, SDL_CONTROLLERDEVICEREMOVED's event.cdevice.which
    *is already* the stable joystick instance ID -- do not treat it as a
    device index, or a second controller could get closed by mistake.
    """
    instance_id = event.cdevice.which
    handle = _open_controllers.pop(instance_id, None)
    if handle:
        sdl2.SDL_GameControllerClose(handle)
    _sync_has_gamepad_flag(io)


def handle_shoulder_button(event, io) -> None:
    """The vendored backend only translates A/B/X/Y/D-pad -- feed L1/R1
    through too, the same add_key_event shape it already uses for those.
    """
    if event.type not in (sdl2.SDL_CONTROLLERBUTTONDOWN, sdl2.SDL_CONTROLLERBUTTONUP):
        return
    button = event.cbutton.button
    is_pressed = event.type == sdl2.SDL_CONTROLLERBUTTONDOWN
    if button == sdl2.SDL_CONTROLLER_BUTTON_LEFTSHOULDER:
        io.add_key_event(imgui.Key.gamepad_l1, is_pressed)
    elif button == sdl2.SDL_CONTROLLER_BUTTON_RIGHTSHOULDER:
        io.add_key_event(imgui.Key.gamepad_r1, is_pressed)


def poll_left_stick_nav(io) -> None:
    """Called once per frame (not per-event) -- ImGui's nav-repeat timer
    needs the *current* held value every frame, and SDL_CONTROLLERAXISMOTION
    only fires on change, so a stick held at constant deflection would emit
    one event and then go silent. If multiple controllers are open, combine
    by taking the max-magnitude value per axis so one idle second controller
    can't fight the one actually being used.
    """
    if not _open_controllers:
        return
    x = 0.0
    y = 0.0
    for handle in _open_controllers.values():
        raw_x = sdl2.SDL_GameControllerGetAxis(handle, sdl2.SDL_CONTROLLER_AXIS_LEFTX)
        raw_y = sdl2.SDL_GameControllerGetAxis(handle, sdl2.SDL_CONTROLLER_AXIS_LEFTY)
        axis_x = _apply_deadzone(raw_x)
        axis_y = _apply_deadzone(raw_y)
        if abs(axis_x) > abs(x):
            x = axis_x
        if abs(axis_y) > abs(y):
            y = axis_y

    io.add_key_analog_event(imgui.Key.gamepad_l_stick_left, x < 0, max(0.0, -x))
    io.add_key_analog_event(imgui.Key.gamepad_l_stick_right, x > 0, max(0.0, x))
    # SDL's Y axis is positive-down; ImGui's l_stick_up expects a positive
    # magnitude for "pushed up" (negative Y).
    io.add_key_analog_event(imgui.Key.gamepad_l_stick_up, y < 0, max(0.0, -y))
    io.add_key_analog_event(imgui.Key.gamepad_l_stick_down, y > 0, max(0.0, y))
