import re
import unittest
from pathlib import Path

from imgui_bundle import imgui

_ROOT = Path(__file__).resolve().parents[1]
_KEY_REF = re.compile(r"imgui\.Key\.([A-Za-z_0-9]+)")
_FLAG_REF = re.compile(
    r"imgui\.(WindowFlags_|PopupFlags_|SelectableFlags_|InputTextFlags_|BackendFlags_|ConfigFlags_)\.([A-Za-z_0-9]+)"
)


def _source_files():
    for sub in ("ui", "client"):
        yield from (_ROOT / sub).rglob("*.py")
    main_py = _ROOT / "main.py"
    if main_py.exists():
        yield main_py


class ImguiKeyNameTests(unittest.TestCase):
    def test_every_referenced_key_exists(self) -> None:
        missing = []
        for path in _source_files():
            for name in _KEY_REF.findall(path.read_text(encoding="utf-8")):
                if not hasattr(imgui.Key, name):
                    missing.append(f"{path.relative_to(_ROOT)}: {name}")
        self.assertEqual(missing, [])

    def test_every_referenced_flag_exists(self) -> None:
        missing = []
        for path in _source_files():
            for enum_name, member in _FLAG_REF.findall(path.read_text(encoding="utf-8")):
                enum_obj = getattr(imgui, enum_name, None)
                if enum_obj is None or not hasattr(enum_obj, member):
                    missing.append(f"{path.relative_to(_ROOT)}: {enum_name}.{member}")
        self.assertEqual(missing, [])

    def test_stub_rejects_unknown_key(self) -> None:
        with self.assertRaises(AttributeError):
            imgui.Key.gamepad_face_top
