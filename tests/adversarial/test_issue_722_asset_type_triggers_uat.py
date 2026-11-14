"""Issue #722: L2/R2 switches asset-type tabs while Search Assets stays selected.

Quoted requirements:
- "L2/R2 = sub-tab (Swarm tabs, asset type tabs)."
- "[ Games ] [ Movies & Shows ] [ Music ]          (L2/R2 or D-pad to switch)"
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
PORTS = ROOT / "ports-client"
if str(PORTS) not in sys.path:
    sys.path.insert(0, str(PORTS))


def _install_imgui() -> mock.MagicMock:
    imgui = mock.MagicMock()
    imgui.button.return_value = False
    imgui.is_popup_open.return_value = False
    imgui.checkbox.return_value = (False, False)
    imgui.input_text.return_value = (False, "")
    imgui.begin_combo.return_value = False
    imgui.begin_child.return_value = None
    bundle = mock.MagicMock()
    bundle.imgui = imgui
    sys.modules["imgui_bundle"] = bundle
    return imgui


_IMGUI = _install_imgui()

import ui.screens.swarm as swarm  # noqa: E402
from ui.screens.swarm import SwarmScreen  # noqa: E402


class _Client:
    def get(self, path: str, *, timeout: float | None = None):
        raise AssertionError(f"L2/R2 handling issued a network call: {path}")

    def post(self, path: str, payload=None, *, timeout: float | None = None):
        raise AssertionError(f"L2/R2 handling issued a network call: {path}")


class AssetTypeTriggerUat(unittest.TestCase):
    def _screen(self, tab: str) -> SwarmScreen:
        screen = SwarmScreen(_Client())
        screen.tab = tab
        screen._loaded_tabs.update(key for key, _label in swarm._TABS)
        return screen

    def _press(self, screen: SwarmScreen, key_name: str) -> None:
        target = getattr(_IMGUI.Key, key_name)
        _IMGUI.is_popup_open.return_value = False
        _IMGUI.button.return_value = False
        _IMGUI.is_key_pressed.side_effect = lambda key, repeat=False: key == target
        try:
            screen.draw(None)
        except Exception:
            # Later widgets are not under test. L2/R2 is handled before they draw.
            pass
        finally:
            _IMGUI.is_key_pressed.side_effect = None

    def test_r2_on_search_assets_switches_games_to_movies(self) -> None:
        # "L2/R2 = sub-tab (Swarm tabs, asset type tabs)."
        # "[ Games ] [ Movies & Shows ] [ Music ]          (L2/R2 or D-pad to switch)"
        screen = self._screen(swarm._TAB_SEARCH)
        screen.asset_kind = swarm._SEARCH_KIND_ROMS
        self._press(screen, "gamepad_r2")
        self.assertEqual(screen.tab, swarm._TAB_SEARCH)
        self.assertEqual(screen.asset_kind, swarm._SEARCH_KIND_MOVIES)

    def test_l2_on_search_assets_steps_back_from_movies_to_games(self) -> None:
        # "L2/R2 = sub-tab (Swarm tabs, asset type tabs)."
        screen = self._screen(swarm._TAB_SEARCH)
        screen.asset_kind = swarm._SEARCH_KIND_MOVIES
        self._press(screen, "gamepad_l2")
        self.assertEqual(screen.tab, swarm._TAB_SEARCH)
        self.assertEqual(screen.asset_kind, swarm._SEARCH_KIND_ROMS)

    def test_r2_off_search_assets_still_switches_swarm_tabs(self) -> None:
        # "L2/R2 = sub-tab (Swarm tabs, asset type tabs)."
        screen = self._screen(swarm._TAB_OVERVIEW)
        self._press(screen, "gamepad_r2")
        self.assertEqual(screen.tab, swarm._TAB_TAILNET)


if __name__ == "__main__":
    unittest.main()
