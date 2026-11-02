"""Issue #677: the source URL handed to the browser must be on a listener that serves it."""
import unittest
from types import SimpleNamespace

from app.web.api_routes import ApiRoutesMixin
from app.web.handlers_media_browse import HandlersMediaBrowseMixin

PEER = {
    "drone_id": "aa:bb:cc:dd:ee:ff", "scheme": "https",
    "api_port": 8443, "peer_mtls_port": 8543,
    "reachable_url": "https://192.168.1.50:8443",
}


class _Handler(HandlersMediaBrowseMixin):
    def __init__(self):
        self.sent = None

    def _media_sources(self):
        return [SimpleNamespace(drone_id=PEER["drone_id"], name="Source", is_local=False)]

    def _media_peer(self, drone_id):
        return dict(PEER)

    def _send_json(self, status, payload):
        self.sent = (status, payload)


class _Router(ApiRoutesMixin):
    def __init__(self):
        self.server = SimpleNamespace(
            is_peer_mtls_listener=True,
            all_tls_servers=[SimpleNamespace(is_peer_mtls_listener=True)],
        )
        self.status = None

    def _send_json(self, status, payload):
        self.status = status


class SourceUrlListenerSurface(unittest.TestCase):
    def test_advertised_source_address_is_not_the_mtls_only_listener(self):
        # "When streaming an asset like a movie from batocera UI, and that movie is a
        # remote asset, it should always stream directly from the source" -- the browser
        # URL is <address>/v1/api/media-browse/.../stream, a browser/admin route.
        # Confidence: medium.
        handler = _Handler()
        handler._handle_media_browse_sources()
        address = handler.sent[1]["sources"][0]["address"]
        port = int(address.rsplit(":", 1)[1])
        router = _Router()
        rejected = False
        if port == PEER["peer_mtls_port"]:
            rejected = router._reject_if_wrong_listener_surface(
                ["media-browse", "movies", PEER["drone_id"], "a1", "stream"],
                "/v1/api/media-browse/movies/x/a1/stream",
            )
        self.assertFalse(rejected, f"{address} is the peer-mTLS listener; it answers {router.status} for the stream route")
        self.assertEqual(port, PEER["api_port"])


if __name__ == "__main__":
    unittest.main()
