"""Regression tests for shared native-client widgets."""

import unittest
from unittest import mock

from imgui_bundle import imgui

from ui import widgets


class LoadingPanelTests(unittest.TestCase):
    def test_submits_layout_item_after_restoring_parent_cursor(self) -> None:
        """ImGui 1.92 asserts if SetCursorPos is the final window operation."""
        events = []
        previous = imgui.ImVec2(14.0, 28.0)

        with (
            mock.patch.object(widgets.imgui, "get_cursor_pos", return_value=previous),
            mock.patch.object(widgets.imgui, "get_window_width", return_value=800.0),
            mock.patch.object(
                widgets.imgui,
                "set_cursor_pos",
                side_effect=lambda _value: events.append("set_cursor_pos"),
            ),
            mock.patch.object(widgets.imgui, "begin_child", return_value=True),
            mock.patch.object(widgets.imgui, "set_cursor_pos_y"),
            mock.patch.object(widgets.imgui, "set_cursor_pos_x"),
            mock.patch.object(widgets, "spinner"),
            mock.patch.object(widgets.imgui, "same_line"),
            mock.patch.object(widgets.imgui, "text"),
            mock.patch.object(widgets.imgui, "end_child"),
            mock.patch.object(
                widgets.imgui,
                "dummy",
                side_effect=lambda _size: events.append("dummy"),
            ),
        ):
            widgets.loading_panel("Loading Swarm...")

        self.assertEqual(events[-2:], ["set_cursor_pos", "dummy"])


if __name__ == "__main__":
    unittest.main()
