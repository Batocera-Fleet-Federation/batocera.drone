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

    imgui = mock.MagicMock()
    imgui.ImVec2 = _ImVec2
    imgui.ImVec4 = _ImVec4
    imgui.WindowFlags_ = _WindowFlags

    bundle = types.ModuleType("imgui_bundle")
    bundle.imgui = imgui
    bundle.hello_imgui = mock.MagicMock()
    sys.modules["imgui_bundle"] = bundle


_ensure_imgui_stub()
