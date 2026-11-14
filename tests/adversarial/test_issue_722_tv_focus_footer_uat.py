"""Issue #722: TV readability, focus ring, and a button-hint footer on every screen.

Quoted requirements:
- "Button-hint footer on every screen showing the active mappings."
- "a larger base font/scale and row heights for 720p/1080p TVs, a clear focus ring on the focused item (theme `ACCENT`)"
- "text is readable on a TV at 720p."
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ports-client" / "ui"
SCREENS = ("login.py", "about.py", "swarm.py", "vpn.py", "backups.py")


def _draw_has_footer(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "draw":
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                text = child.value.lower()
                if "back" in text and ("select" in text or " b " in f" {text} "):
                    return True
            if isinstance(child, ast.Call):
                func = child.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if name and re.search(r"hint|footer", name, re.IGNORECASE):
                    return True
    return False


def _ui_text() -> str:
    parts = []
    for path in UI.rglob("*.py"):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


class TvFocusFooterUat(unittest.TestCase):
    def test_every_screen_draws_a_button_hint_footer(self) -> None:
        # "Button-hint footer on every screen showing the active mappings."
        missing = [name for name in SCREENS if not _draw_has_footer(UI / "screens" / name)]
        self.assertEqual(missing, [])

    def test_focus_ring_uses_theme_accent(self) -> None:
        # "a clear focus ring on the focused item (theme `ACCENT`)"
        theme = (UI / "theme.py").read_text(encoding="utf-8")
        self.assertRegex(theme, r"nav_(highlight|cursor)")
        window = 400
        for match in re.finditer(r"nav_(highlight|cursor)", theme):
            snippet = theme[max(0, match.start() - window) : match.end() + window]
            if "ACCENT" in snippet or "#00c2ff" in snippet.lower():
                return
        self.fail("focus ring color is not theme ACCENT")

    def test_base_font_and_row_height_are_larger_for_tv(self) -> None:
        # "a larger base font/scale and row heights for 720p/1080p TVs"
        # "text is readable on a TV at 720p."
        text = _ui_text()
        larger_font = False
        for pattern, minimum in (
            (r"font_global_scale\s*=\s*([0-9.]+)", 1.0),
            (r"font_scale_main\s*=\s*([0-9.]+)", 1.0),
            (r"scale_all_sizes\(\s*([0-9.]+)\s*\)", 1.0),
        ):
            for match in re.finditer(pattern, text):
                if float(match.group(1)) > minimum:
                    larger_font = True
        for match in re.finditer(r"add_font_[a-z0-9_]+\([^)\n]*,\s*([0-9.]+)", text):
            if float(match.group(1)) >= 18:
                larger_font = True
        self.assertTrue(larger_font, "base font/scale is still the desktop default")

        larger_rows = False
        for match in re.finditer(
            r"frame_padding\s*=\s*[A-Za-z0-9_\.]+\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*\)",
            text,
        ):
            if float(match.group(2)) > 6.0:
                larger_rows = True
        for match in re.finditer(r"(?i)row_height(?:_px)?\s*=\s*([0-9.]+)", text):
            if float(match.group(1)) >= 28:
                larger_rows = True
        self.assertTrue(larger_rows, "row height is still the pre-TV padding")


if __name__ == "__main__":
    unittest.main()
