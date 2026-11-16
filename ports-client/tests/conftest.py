"""Make both ``client`` (this app) and ``app`` (the Drone package, for the
integration test's real server harness) importable regardless of the cwd
pytest was invoked from, and provide a lightweight imgui_bundle stub when
running the unit suite in a stdlib-only environment.
"""

import sys
import types
from pathlib import Path
from unittest import mock

_PORTS_CLIENT_ROOT = Path(__file__).resolve().parents[1]
_DRONE_ROOT = _PORTS_CLIENT_ROOT.parent

for _path in (_PORTS_CLIENT_ROOT, _DRONE_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _ensure_imgui_stub() -> None:
    try:
        import imgui_bundle  # noqa: F401
        return
    except ImportError:
        pass

    class _ImVec2:
        def __init__(self, x: float = 0.0, y: float = 0.0) -> None:
            self.x = float(x)
            self.y = float(y)

    class _ImVec4:
        def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0, w: float = 0.0) -> None:
            self.x = float(x)
            self.y = float(y)
            self.z = float(z)
            self.w = float(w)

    class _Flag:
        def __init__(self, value: int) -> None:
            self.value = value

    class _WindowFlags:
        no_title_bar = _Flag(1 << 0)
        no_resize = _Flag(1 << 1)
        no_move = _Flag(1 << 2)
        no_scrollbar = _Flag(1 << 3)
        no_saved_settings = _Flag(1 << 4)
        no_nav = _Flag(1 << 5)
        no_focus_on_appearing = _Flag(1 << 6)
        always_auto_resize = _Flag(1 << 7)
        no_background = _Flag(1 << 8)
        no_bring_to_front_on_focus = _Flag(1 << 9)
        no_collapse = _Flag(1 << 10)
        no_inputs = _Flag(1 << 11)
        no_decoration = _Flag(1 << 12)
        no_nav_focus = _Flag(1 << 13)

    class _PopupFlags:
        none = _Flag(0)
        any_popup_id = _Flag(1 << 10)

    class _SelectableFlags:
        none = _Flag(0)
        allow_overlap = _Flag(1 << 4)

    class _InputTextFlags:
        none = _Flag(0)
        enter_returns_true = _Flag(1 << 5)
        password = _Flag(1 << 10)

    class _BackendFlags:
        none = _Flag(0)
        has_gamepad = _Flag(1 << 0)

    class _ConfigFlags:
        none = _Flag(0)
        nav_enable_keyboard = _Flag(1 << 0)
        nav_enable_gamepad = _Flag(1 << 1)

    class _Key:
        # Only real imgui_bundle (1.93) Key members, so a typo such as
        # ``gamepad_face_top`` raises AttributeError in tests like on device.
        gamepad_face_left = 1
        gamepad_face_right = 2
        gamepad_face_up = 3
        gamepad_face_down = 4
        gamepad_dpad_left = 5
        gamepad_dpad_right = 6
        gamepad_dpad_up = 7
        gamepad_dpad_down = 8
        gamepad_l1 = 9
        gamepad_r1 = 10
        gamepad_l2 = 11
        gamepad_r2 = 12
        gamepad_l3 = 13
        gamepad_r3 = 14
        gamepad_start = 15
        gamepad_back = 16
        gamepad_l_stick_up = 17
        gamepad_l_stick_down = 18
        gamepad_l_stick_left = 19
        gamepad_l_stick_right = 20
        gamepad_r_stick_up = 21
        gamepad_r_stick_down = 22
        gamepad_r_stick_left = 23
        gamepad_r_stick_right = 24
        enter = 25
        escape = 26
        backspace = 27

    imgui = mock.MagicMock()
    imgui.Key = _Key
    imgui.ImVec2 = _ImVec2
    imgui.ImVec4 = _ImVec4
    imgui.WindowFlags_ = _WindowFlags
    imgui.PopupFlags_ = _PopupFlags
    imgui.SelectableFlags_ = _SelectableFlags
    imgui.InputTextFlags_ = _InputTextFlags
    imgui.BackendFlags_ = _BackendFlags
    imgui.ConfigFlags_ = _ConfigFlags

    bundle = types.ModuleType("imgui_bundle")
    bundle.imgui = imgui
    bundle.hello_imgui = mock.MagicMock()
    sys.modules["imgui_bundle"] = bundle


_ensure_imgui_stub()
