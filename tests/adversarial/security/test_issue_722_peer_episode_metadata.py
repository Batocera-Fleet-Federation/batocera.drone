"""Regression: paired peer episode metadata must not terminate Ports rendering."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[3]


class PeerEpisodeMetadataTests(unittest.TestCase):
    def test_peer_inventory_cannot_crash_search_results_with_nonnumeric_season(self):
        """Regression: untrusted episode season crashes Search Assets. Confidence: high."""
        program = textwrap.dedent('''
            import sys
            import types
            from unittest import mock
            from app.transfer import media_browse
            from app.web import handlers_media_browse
            from app.web.handlers_network import HandlersNetworkMixin

            # Only the native drawing toolkit is stubbed; use the real browse
            # fetch, merge, endpoint wrapper, page application and row renderer.
            bundle = types.ModuleType("imgui_bundle")
            bundle.imgui = mock.MagicMock()
            bundle.imgui.selectable.return_value = (False, False)
            sys.modules["imgui_bundle"] = bundle
            sys.path.insert(0, "ports-client")
            from ui.screens.swarm import SwarmScreen

            class Handler(handlers_media_browse.HandlersMediaBrowseMixin, HandlersNetworkMixin):
                settings = types.SimpleNamespace(use_fake_data=False)
                def _media_sources(self, only=None):
                    return [media_browse.MediaSource("paired-attacker", "Paired Drone")]
                def _media_peer(self, drone_id):
                    return {"drone_id": drone_id}
                def _local_fingerprints(self, kind):
                    return set()
                def _send_json(self, status, payload):
                    assert status == 200, payload
                    self.payload = payload

            malicious = {
                "total": 1,
                "items": [{"display_title": "An Episode", "show_title": "A Show",
                           "season": "not-a-number", "episode": 1,
                           "file_path": "episode.mkv", "file_size": 100}]
            }
            handler = Handler()
            with mock.patch.object(handlers_media_browse, "_source_cache", media_browse.SourceCache()), \
                    mock.patch("app.web.handlers_network._peer_get_json_for_peer", return_value=(malicious, "fixture")):
                handler._handle_media_browse_list("movies", {"drones": ["paired-attacker"]})
            assert handler.payload["items"][0]["season"] == "not-a-number"

            class Client:
                def get(self, path, **kwargs):
                    if path == "/media-browse/sources":
                        return {"sources": [{"drone_id": "paired-attacker", "online": True}]}
                    return handler.payload

            screen = SwarmScreen(Client())
            screen.asset_kind = "movies"
            screen._reload_search()
            assert screen.browse_items[0]["drone_id"] == "paired-attacker"
            screen._draw_search_result_row(0, screen.browse_items[0])
        ''')
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(
            result.returncode, 0,
            "A paired peer's inventory escaped the render loop as an exception:\n" + result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
