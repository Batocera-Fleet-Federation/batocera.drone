"""UAT coverage for issues #754/#755: Search Assets gamepad Y/X/B keys and modal gating."""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_PORTS = _ROOT / "ports-client"
if str(_PORTS) not in sys.path:
    sys.path.insert(0, str(_PORTS))


def _load_ports_imgui_stub() -> None:
    if "imgui_bundle" in sys.modules:
        return
    conftest_path = _PORTS / "tests" / "conftest.py"
    spec = importlib.util.spec_from_file_location("_ports_client_conftest", conftest_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


_load_ports_imgui_stub()

from imgui_bundle import imgui  # noqa: E402
from ui.screens import swarm as swarm_module  # noqa: E402
from ui.screens.swarm import SwarmScreen  # noqa: E402


class _FakeSearchClient:
    def __init__(self) -> None:
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict | None]] = []

    def get(self, path: str, *, timeout: float | None = None):
        self.gets.append(path)
        if "/media-browse/sources" in path:
            return {
                "sources": [
                    {"drone_id": "peer-arcade", "name": "Arcade", "is_local": False, "online": True}
                ]
            }
        if "/media-browse/" in path:
            return {
                "items": [
                    {
                        "display_title": "Sonic the Hedgehog",
                        "name": "Sonic the Hedgehog",
                        "system": "megadrive",
                        "file_path": "megadrive/Sonic.zip",
                        "file_size": 524288,
                        "drone_id": "peer-arcade",
                        "drone_name": "Arcade",
                        "exists_locally": False,
                    }
                ],
                "sources": [{"drone_id": "peer-arcade", "name": "Arcade", "status": "ok"}],
                "next_cursor": "",
                "has_more": False,
            }
        return {"active": [], "queued": [], "recent": [], "downloads": []}

    def post(self, path: str, body=None, *, timeout: float | None = None):
        self.posts.append((path, body if isinstance(body, dict) else {}))
        return {"jobs": [{"job_id": "job-755", "status": "queued"}]}


class SearchAssetsGamepadUatTests(unittest.TestCase):
    def _draw_search_frame(self, screen: SwarmScreen, *, pressed_key=None, popup_open: bool = False) -> None:
        ui_imgui = swarm_module.imgui
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(swarm_module, "search_box", return_value=("", False)))
            stack.enter_context(mock.patch.object(swarm_module, "tab_button"))
            stack.enter_context(mock.patch.object(ui_imgui, "checkbox", return_value=(False, False)))
            stack.enter_context(mock.patch.object(ui_imgui, "begin_combo", return_value=False))
            stack.enter_context(mock.patch.object(ui_imgui, "button", return_value=False))
            stack.enter_context(mock.patch.object(ui_imgui, "get_scroll_max_y", return_value=0.0))
            stack.enter_context(mock.patch.object(ui_imgui, "is_popup_open", return_value=popup_open))
            stack.enter_context(mock.patch.object(ui_imgui, "selectable", return_value=(False, False)))
            stack.enter_context(mock.patch.object(ui_imgui, "is_item_focused", return_value=False))
            stack.enter_context(mock.patch.object(ui_imgui, "begin_popup_modal", return_value=(True, True)))
            stack.enter_context(
                mock.patch.object(
                    ui_imgui,
                    "is_key_pressed",
                    side_effect=lambda key, *a: key == pressed_key,
                )
            )
            screen.draw(None)

    def test_search_assets_keys_exist_and_invalid_gamepad_face_top_is_rejected(self) -> None:
        key_ref = re.compile(r"imgui\.Key\.([A-Za-z_0-9]+)")
        missing = []
        for sub in ("ui", "client"):
            for path in (_PORTS / sub).rglob("*.py"):
                for name in key_ref.findall(path.read_text(encoding="utf-8")):
                    if not hasattr(imgui.Key, name):
                        missing.append(f"{path.relative_to(_PORTS)}: {name}")
        self.assertEqual(missing, [])
        with self.assertRaises(AttributeError):
            _ = imgui.Key.gamepad_face_top

    def test_gamepad_y_opens_details_modal_x_blocked_while_open_b_closes_then_x_downloads(self) -> None:
        client = _FakeSearchClient()
        screen = SwarmScreen(client)
        screen._queue_tab(swarm_module._TAB_SEARCH, "Search Assets")
        screen.wait_for_deferred_action(timeout=2)
        self.assertEqual(len(screen.browse_items), 1)
        screen.focused_row_index = 0

        ui_imgui = swarm_module.imgui
        # 1. Pressing Y opens the Details modal without crashing on invalid imgui.Key.
        with mock.patch.object(ui_imgui, "open_popup") as open_popup:
            self._draw_search_frame(screen, pressed_key=ui_imgui.Key.gamepad_face_up, popup_open=False)
        self.assertIs(screen.details_item, screen.browse_items[0])
        open_popup.assert_called_once_with("Asset Details")

        # 2. Pressing X while a modal popup is open must not trigger background download.
        self._draw_search_frame(screen, pressed_key=ui_imgui.Key.gamepad_face_left, popup_open=True)
        self.assertIsNone(screen.deferred_action_label)
        self.assertEqual(client.posts, [])

        # 3. Pressing B inside Details closes the modal.
        with mock.patch.object(ui_imgui, "close_current_popup") as close_popup:
            self._draw_search_frame(screen, pressed_key=ui_imgui.Key.gamepad_face_right, popup_open=True)
        close_popup.assert_called_once_with()
        self.assertIsNone(screen.details_item)

        # 4. Pressing X with no popup open downloads the focused row.
        self._draw_search_frame(screen, pressed_key=ui_imgui.Key.gamepad_face_left, popup_open=False)
        screen.wait_for_deferred_action(timeout=2)
        self.assertEqual(len(client.posts), 1)
        self.assertTrue(client.posts[0][0].endswith("/admin/local-network/sync"))
        self.assertEqual(client.posts[0][1]["peer_id"], "peer-arcade")


if __name__ == "__main__":
    unittest.main()
