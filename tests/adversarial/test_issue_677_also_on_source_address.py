"""Issue #677: detail-page remote playback must carry the source address."""

import unittest
from types import SimpleNamespace
from unittest import mock

from app.transfer.media_browse import MediaSource
from app.web import handlers_media_browse
from app.web.handlers_media_browse import HandlersMediaBrowseMixin


PEER = {
    "drone_id": "aa:bb:cc:dd:ee:ff",
    "scheme": "https",
    "api_port": 8443,
    "peer_mtls_port": 8543,
    "reachable_url": "https://192.168.1.50:8443",
}


class _Handler(HandlersMediaBrowseMixin):
    def __init__(self):
        self.sent = None

    def _media_sources(self, only=None):
        return [MediaSource(drone_id=PEER["drone_id"], name="Source", is_local=False)]

    def _media_peer(self, drone_id):
        return dict(PEER)

    def _send_json(self, status, payload):
        self.sent = (status, payload)


class AvailableOnSourceAddress(unittest.TestCase):
    def test_remote_copy_from_available_on_has_direct_source_address(self):
        # "When streaming an asset like a movie from batocera UI, and that movie
        # is a remote asset, it should always stream directly from the source
        # rather than proxying through the drone being used." Confidence: high.
        page = {
            "items": [{
                "drone_id": PEER["drone_id"],
                "entry_key": "a1",
                "display_title": "Remote movie",
                "is_local": False,
            }],
            "sources": [],
            "has_more": False,
            "next_cursor": "",
            "total": 1,
        }
        handler = _Handler()
        with mock.patch.object(handlers_media_browse._media_browse, "collect_page", return_value=page):
            handler._handle_media_browse_also_on("movies", {"title": ["Remote movie"]})

        self.assertEqual(handler.sent[0], 200)
        copy = handler.sent[1]["copies"][0]
        self.assertEqual(copy["address"], "https://192.168.1.50:8443")


if __name__ == "__main__":
    unittest.main()
