"""Issue #722: L2/R2 trigger axes switch sub-tabs.

Quoted requirements:
- "L2/R2 = sub-tab (Swarm tabs, asset type tabs)."
- "Note: `ui/gamepad.py` only feeds L1/R1 through today, so L2/R2 (trigger axes) need adding."
- "L1/R1 and L2/R2 switch sections and sub-tabs, and B backs out of popups/lists."
"""

from __future__ import annotations

import ast
import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
PORTS = ROOT / "ports-client"
APP_PY = PORTS / "ui" / "app.py"
if str(PORTS) not in sys.path:
    sys.path.insert(0, str(PORTS))

SDL_CONTROLLER_AXIS_LEFTX = 0
SDL_CONTROLLER_AXIS_LEFTY = 1
SDL_CONTROLLER_AXIS_TRIGGERLEFT = 4
SDL_CONTROLLER_AXIS_TRIGGERRIGHT = 5


def _install_stubs() -> None:
    try:
        import sdl2  # noqa: F401
        from imgui_bundle import imgui  # noqa: F401
        return
    except ImportError:
        pass
    import types

    sdl2 = types.ModuleType("sdl2")
    sdl2.SDL_CONTROLLER_AXIS_LEFTX = SDL_CONTROLLER_AXIS_LEFTX
    sdl2.SDL_CONTROLLER_AXIS_LEFTY = SDL_CONTROLLER_AXIS_LEFTY
    sdl2.SDL_CONTROLLER_AXIS_TRIGGERLEFT = SDL_CONTROLLER_AXIS_TRIGGERLEFT
    sdl2.SDL_CONTROLLER_AXIS_TRIGGERRIGHT = SDL_CONTROLLER_AXIS_TRIGGERRIGHT
    sdl2.SDL_GameControllerGetAxis = lambda handle, axis: 0
    sys.modules["sdl2"] = sdl2

    class _Keys:
        gamepad_l1 = "gamepad_l1"
        gamepad_r1 = "gamepad_r1"
        gamepad_l2 = "gamepad_l2"
        gamepad_r2 = "gamepad_r2"
        gamepad_l_stick_left = "stick_left"
        gamepad_l_stick_right = "stick_right"
        gamepad_l_stick_up = "stick_up"
        gamepad_l_stick_down = "stick_down"
        gamepad_face_right = "face_right"

    class _Flags:
        class has_gamepad:
            value = 1

    imgui = types.ModuleType("imgui")
    imgui.Key = _Keys()
    imgui.BackendFlags_ = _Flags()
    bundle = types.ModuleType("imgui_bundle")
    bundle.imgui = imgui
    sys.modules["imgui_bundle"] = bundle
    sys.modules["imgui"] = imgui


_install_stubs()

import sdl2  # noqa: E402
from imgui_bundle import imgui  # noqa: E402
from ui import gamepad  # noqa: E402


class _IO:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.backend_flags = 0

    def add_key_event(self, key, pressed) -> None:
        self.events.append(("key", key, bool(pressed)))

    def add_key_analog_event(self, key, pressed, value) -> None:
        self.events.append(("analog", key, bool(pressed), float(value)))


def _frame_poll_names() -> list[str]:
    source = APP_PY.read_text(encoding="utf-8")
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "PortsClientApp":
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == "_run_loop":
                    names = []
                    for call in ast.walk(child):
                        if not isinstance(call, ast.Call):
                            continue
                        func = call.func
                        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "gamepad":
                            names.append(func.attr)
                    return names
    return []


def _poll(io: _IO) -> None:
    called = False
    for name in _frame_poll_names():
        function = getattr(gamepad, name, None)
        if function is None:
            continue
        try:
            parameters = [parameter for parameter in inspect.signature(function).parameters.values()]
        except (TypeError, ValueError):
            continue
        if len(parameters) == 1:
            function(io)
            called = True
    if not called:
        raise AssertionError(f"frame loop does not poll gamepad axes: {_frame_poll_names()}")


def _pressed(io: _IO, key) -> bool:
    return any(pressed and event_key == key for _kind, event_key, pressed, *_rest in io.events)


class GamepadTriggersUat(unittest.TestCase):
    def setUp(self) -> None:
        gamepad._open_controllers.clear()
        gamepad._open_controllers[1] = object()
        self.axes = {
            SDL_CONTROLLER_AXIS_LEFTX: 0,
            SDL_CONTROLLER_AXIS_LEFTY: 0,
            SDL_CONTROLLER_AXIS_TRIGGERLEFT: 0,
            SDL_CONTROLLER_AXIS_TRIGGERRIGHT: 0,
        }

        def get_axis(_handle, axis):
            return self.axes.get(int(axis), 0)

        self._patch = mock.patch.object(sdl2, "SDL_GameControllerGetAxis", side_effect=get_axis)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(gamepad._open_controllers.clear)

    def test_resting_triggers_do_not_register(self) -> None:
        # "L2/R2 (trigger axes) need adding."
        io = _IO()
        _poll(io)
        self.assertFalse(_pressed(io, imgui.Key.gamepad_l2))
        self.assertFalse(_pressed(io, imgui.Key.gamepad_r2))

    def test_full_left_and_right_triggers_are_independent(self) -> None:
        # "L2/R2 = sub-tab (Swarm tabs, asset type tabs)."
        io = _IO()
        self.axes[SDL_CONTROLLER_AXIS_TRIGGERLEFT] = 32767
        _poll(io)
        self.assertTrue(_pressed(io, imgui.Key.gamepad_l2), io.events)
        self.assertFalse(_pressed(io, imgui.Key.gamepad_r2), io.events)

        io = _IO()
        self.axes[SDL_CONTROLLER_AXIS_TRIGGERLEFT] = 0
        self.axes[SDL_CONTROLLER_AXIS_TRIGGERRIGHT] = 32767
        _poll(io)
        self.assertFalse(_pressed(io, imgui.Key.gamepad_l2), io.events)
        self.assertTrue(_pressed(io, imgui.Key.gamepad_r2), io.events)

        io = _IO()
        self.axes[SDL_CONTROLLER_AXIS_TRIGGERLEFT] = 32767
        self.axes[SDL_CONTROLLER_AXIS_TRIGGERRIGHT] = 32767
        _poll(io)
        self.assertTrue(_pressed(io, imgui.Key.gamepad_l2), io.events)
        self.assertTrue(_pressed(io, imgui.Key.gamepad_r2), io.events)

    def test_swarm_screen_uses_l2_r2_for_subtabs(self) -> None:
        # "L2/R2 = sub-tab (Swarm tabs, asset type tabs)."
        source = (PORTS / "ui" / "screens" / "swarm.py").read_text(encoding="utf-8")
        self.assertIn("gamepad_l2", source)
        self.assertIn("gamepad_r2", source)
        module = ast.parse(source)
        tab_labels = []
        for node in module.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "_TABS" and isinstance(node.value, (ast.Tuple, ast.List)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Tuple) and len(elt.elts) >= 2 and isinstance(elt.elts[1], ast.Constant):
                                tab_labels.append(elt.elts[1].value)
        self.assertEqual(tab_labels, ["Overview", "Tailnet", "Local Network", "Search Assets"])


if __name__ == "__main__":
    unittest.main()
