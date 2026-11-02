"""Adversarial coverage for Issue #677 remote media playback."""

from pathlib import Path
import unittest


class Issue677RemoteMediaPlaybackUAT(unittest.TestCase):
    def test_remote_media_keeps_play_action_without_drone_proxy_label(self) -> None:
        script = (
            Path(__file__).resolve().parents[2]
            / "app"
            / "web"
            / "static"
            / "js"
            / "media_browse.js"
        ).read_text(encoding="utf-8")

        # Issue #677: "When streaming an asset like a movie from batocera UI, and
        # that movie is a remote asset, it should always stream directly from the
        # source rather than proxying through the drone being used. Remove the
        # \"play from {drone}\" button as this will no longer be needed."
        # Confidence: high.
        self.assertIn('const play = kind !== "roms" && it.entry_key', script)
        self.assertNotIn('const play = kind !== "roms" && it.entry_key && it.is_local', script)
        self.assertNotIn('" from " + esc(droneLabel(it))', script)


if __name__ == "__main__":
    unittest.main()
