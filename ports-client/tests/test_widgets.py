"""Regression tests for shared native-client widgets."""

import unittest
from unittest import mock

from imgui_bundle import imgui

from ui import widgets


class LoadingPanelTests(unittest.TestCase):
    def test_overlay_does_not_scroll_the_parent_and_still_finalizes_layout(self) -> None:
        """The panel is a viewport overlay. A layout child at content y=72 made
        ImGui scroll the page to the top for every frame it was open. Inside
        the overlay, SetCursorPos still cannot be the last layout op (ImGui
        1.92 asserts)."""
        events = []
        begun = {}

        def _begin(name, flags=0):
            begun["name"] = name
            begun["flags"] = flags
            events.append("begin")
            return True, False

        with (
            mock.patch.object(widgets.imgui, "get_window_pos", return_value=imgui.ImVec2(0.0, 0.0)),
            mock.patch.object(widgets.imgui, "get_window_width", return_value=800.0),
            mock.patch.object(widgets.imgui, "set_next_window_pos", side_effect=lambda _pos: events.append("set_next_window_pos")),
            mock.patch.object(widgets.imgui, "set_next_window_size", side_effect=lambda _size: events.append("set_next_window_size")),
            mock.patch.object(widgets.imgui, "begin", side_effect=_begin),
            mock.patch.object(widgets.imgui, "end", side_effect=lambda: events.append("end")),
            mock.patch.object(widgets.imgui, "set_cursor_pos", side_effect=lambda _value: events.append("set_cursor_pos")),
            mock.patch.object(widgets, "spinner"),
            mock.patch.object(widgets.imgui, "same_line"),
            mock.patch.object(widgets.imgui, "text"),
            mock.patch.object(widgets.imgui, "dummy", side_effect=lambda _size: events.append("dummy")),
        ):
            widgets.loading_panel("Loading Swarm...")

        self.assertEqual(begun["name"], "##global_loading_panel")
        self.assertEqual(events[:2], ["set_next_window_pos", "set_next_window_size"])
        self.assertEqual(events[-3:], ["set_cursor_pos", "dummy", "end"])
        no_nav = widgets.imgui.WindowFlags_.no_nav.value
        no_focus = widgets.imgui.WindowFlags_.no_focus_on_appearing.value
        self.assertTrue(begun["flags"] & no_nav)
        self.assertTrue(begun["flags"] & no_focus)


if __name__ == "__main__":
    unittest.main()
