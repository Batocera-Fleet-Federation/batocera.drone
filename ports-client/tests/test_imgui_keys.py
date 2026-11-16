import re
import unittest
from pathlib import Path

from imgui_bundle import imgui

_ROOT = Path(__file__).resolve().parents[1]
_KEY_REF = re.compile(r"imgui\.Key\.([A-Za-z_0-9]+)")


class ImguiKeyNameTests(unittest.TestCase):
    def test_every_referenced_key_exists(self) -> None:
        missing = []
        for sub in ("ui", "client"):
            for path in (_ROOT / sub).rglob("*.py"):
                for name in _KEY_REF.findall(path.read_text(encoding="utf-8")):
                    if not hasattr(imgui.Key, name):
                        missing.append(f"{path.relative_to(_ROOT)}: {name}")
        self.assertEqual(missing, [])

    def test_stub_rejects_unknown_key(self) -> None:
        with self.assertRaises(AttributeError):
            imgui.Key.gamepad_face_top
