"""Remote-drone administration: the generic peer-admin proxy and its handlers.

Covers ``app.transfer.peer_connectivity._peer_proxy_request`` (the network
primitive: address iteration, pinned-TLS reuse, mutating-request single-address
safety) and ``app.web.handlers_remote_admin.HandlersRemoteAdminMixin`` (session
cache, connect/disconnect/status, the generic proxy dispatch and its error
mapping), plus the frontend's tab-scoped impersonation wiring in drone.js.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from app.drone_api import Settings


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "OVERMIND_DEVICE_ID": "gateway-drone",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


PEER = {
    "drone_id": "peer-b",
    "name": "Living Room",
    "reachable_url": "https://192.168.1.50",
    "scheme": "https",
    "api_port": 443,
}


class PeerProxyRequestTests(unittest.TestCase):
    """`_peer_proxy_request`: address iteration, trust reuse, and the
    single-address/no-retry rule for mutating requests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = _build_settings(Path(self._tmp.name) / "userdata")
        from app.transfer import peer_connectivity

        # These tests exercise _peer_proxy_request's own logic (addressing,
        # method/header/body construction, error handling) with urlopen
        # mocked out -- real certificate validation is covered elsewhere
        # (_peer_trust_cafile/_drone_client_ssl_context's own tests), so both
        # are stubbed here to avoid needing a real cert file on disk.
        self._trust_patch = mock.patch.object(
            peer_connectivity, "_peer_trust_cafile", return_value=Path(self._tmp.name) / "fake-peer.crt"
        )
        self._ssl_patch = mock.patch.object(peer_connectivity, "_drone_client_ssl_context", return_value=None)
        self._trust_patch.start()
        self._ssl_patch.start()

    def tearDown(self) -> None:
        self._ssl_patch.stop()
        self._trust_patch.stop()
        self._tmp.cleanup()

    def test_get_success_relays_status_content_type_and_body(self) -> None:
        from app.transfer import peer_connectivity

        response = mock.MagicMock()
        response.status = 200
        response.headers.get.return_value = "application/json"
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch.object(peer_connectivity, "urlopen", return_value=response) as opened:
            result = peer_connectivity._peer_proxy_request(
                PEER, "GET", "/v1/api/admin/system-info", self.settings,
                authorization="Basic dGVzdA==", peer_id="peer-b",
            )
        self.assertEqual(result.status, 200)
        self.assertEqual(result.content_type, "application/json")
        self.assertEqual(result.body, b'{"ok": true}')
        request = opened.call_args[0][0]
        self.assertEqual(request.full_url, "https://192.168.1.50/v1/api/admin/system-info")
        self.assertEqual(request.get_header("Authorization"), "Basic dGVzdA==")
        self.assertEqual(request.get_method(), "GET")

    def test_post_uppercases_method_and_sets_content_type_only_with_a_body(self) -> None:
        from app.transfer import peer_connectivity

        response = mock.MagicMock()
        response.status = 200
        response.headers.get.return_value = "application/json"
        response.read.return_value = b"{}"
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch.object(peer_connectivity, "urlopen", return_value=response) as opened:
            peer_connectivity._peer_proxy_request(
                PEER, "post", "/v1/api/admin/controls", self.settings,
                body=b'{"level": 50}', authorization="Basic dGVzdA==",
                content_type="application/json", peer_id="peer-b",
            )
        request = opened.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.data, b'{"level": 50}')

    def test_http_error_is_relayed_not_raised(self) -> None:
        from app.transfer import peer_connectivity

        error = HTTPError("https://192.168.1.50/v1/api/admin/system-info", 401, "Unauthorized", {}, None)
        error.read = mock.Mock(return_value=b'{"error": "unauthorized"}')
        with mock.patch.object(peer_connectivity, "urlopen", side_effect=error):
            result = peer_connectivity._peer_proxy_request(
                PEER, "GET", "/v1/api/admin/system-info", self.settings,
                authorization="Basic bad", peer_id="peer-b",
            )
        self.assertEqual(result.status, 401)
        self.assertEqual(result.body, b'{"error": "unauthorized"}')

    def test_get_falls_back_to_next_candidate_address_on_connection_failure(self) -> None:
        from app.transfer import peer_connectivity

        peer = {**PEER, "reachable_url": "https://192.168.1.50", "tailnet_ip": "100.64.0.9"}
        good_response = mock.MagicMock()
        good_response.status = 200
        good_response.headers.get.return_value = "application/json"
        good_response.read.return_value = b"{}"
        good_response.__enter__.return_value = good_response
        good_response.__exit__.return_value = False
        with mock.patch.object(peer_connectivity, "urlopen", side_effect=[URLError("unreachable"), good_response]) as opened:
            result = peer_connectivity._peer_proxy_request(
                peer, "GET", "/v1/api/admin/system-info", self.settings,
                authorization="Basic dGVzdA==", peer_id="peer-b",
            )
        self.assertEqual(result.status, 200)
        self.assertEqual(opened.call_count, 2)

    def test_post_does_not_retry_a_different_address_on_failure(self) -> None:
        # A mutating request must not be silently re-sent via a second address
        # on a timeout/connection failure -- the peer may have already
        # received and be processing the first attempt, and resending could
        # fire the action twice (e.g. an EmulationStation restart).
        from app.transfer import peer_connectivity

        peer = {**PEER, "reachable_url": "https://192.168.1.50", "tailnet_ip": "100.64.0.9"}
        with mock.patch.object(peer_connectivity, "urlopen", side_effect=URLError("timed out")) as opened:
            with self.assertRaises(URLError):
                peer_connectivity._peer_proxy_request(
                    peer, "POST", "/v1/api/admin/controls", self.settings,
                    body=b"{}", authorization="Basic dGVzdA==", peer_id="peer-b",
                )
        self.assertEqual(opened.call_count, 1)

    def test_no_address_available_raises_value_error(self) -> None:
        from app.transfer import peer_connectivity

        with self.assertRaises(ValueError):
            peer_connectivity._peer_proxy_request({"drone_id": "ghost"}, "GET", "/v1/api/admin/system-info", self.settings)


class _FakeHandler:
    """Minimal stand-in for RomRequestHandler, mixed with the real mixin under
    test -- same pattern as test_es_collections.py's EsCollectionsAdminHandlerTests."""

    def __init__(self, settings: Settings, *, headers=None, body: bytes = b"") -> None:
        self.settings = settings
        self.headers = headers or {}
        self.rfile = mock.Mock()
        self.rfile.read.return_value = body
        self.wfile = mock.Mock()
        self.response = None  # set by _send_json
        self.relayed = None  # set by the raw send_response/... path

    def _send_json(self, status_code: int, payload: dict) -> None:
        self.response = (status_code, payload)

    def _send_security_headers(self) -> None:
        pass

    def send_response(self, status_code: int) -> None:
        self.relayed = {"status": status_code, "headers": {}}

    def send_header(self, name: str, value: str) -> None:
        self.relayed["headers"][name] = value

    def end_headers(self) -> None:
        pass


def _handler(settings: Settings, **kwargs) -> _FakeHandler:
    from app.web import handlers_remote_admin

    class Handler(handlers_remote_admin.HandlersRemoteAdminMixin, _FakeHandler):
        pass

    return Handler(settings, **kwargs)


class RemoteAdminHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = _build_settings(Path(self._tmp.name) / "userdata")
        from app.transfer import local_network

        local_network.save_paired_peer(self.settings, dict(PEER))
        from app.web import handlers_remote_admin

        self._sessions_patch = mock.patch.dict(handlers_remote_admin._REMOTE_SESSIONS, {}, clear=True)
        self._sessions_patch.start()

    def tearDown(self) -> None:
        self._sessions_patch.stop()
        self._tmp.cleanup()

    def _proxy_response(self, status: int, body: bytes = b"{}", content_type: str = "application/json"):
        from app.transfer.peer_connectivity import PeerProxyResponse

        return PeerProxyResponse(status, content_type, body)

    # -- status --------------------------------------------------------

    def test_status_reports_not_connected_before_connect(self) -> None:
        handler = _handler(self.settings)
        handler._handle_admin_remote_status("peer-b")
        status, payload = handler.response
        self.assertEqual(status, 200)
        self.assertFalse(payload["connected"])
        self.assertEqual(payload["name"], "Living Room")

    def test_status_for_unknown_peer_is_not_connected_but_not_an_error(self) -> None:
        handler = _handler(self.settings)
        handler._handle_admin_remote_status("nobody")
        status, payload = handler.response
        self.assertEqual(status, 200)
        self.assertFalse(payload["connected"])

    # -- connect ---------------------------------------------------------

    def test_connect_requires_all_fields(self) -> None:
        handler = _handler(self.settings)
        handler._handle_admin_remote_connect({"peer_id": "peer-b", "username": "admin"})
        self.assertEqual(handler.response[0], 400)

    def test_connect_unknown_peer_is_404(self) -> None:
        handler = _handler(self.settings)
        handler._handle_admin_remote_connect({"peer_id": "ghost", "username": "a", "password": "b"})
        self.assertEqual(handler.response[0], 404)

    def test_connect_success_caches_session_and_returns_version(self) -> None:
        from app.web import handlers_remote_admin

        ok = self._proxy_response(200, json.dumps({"fields": {"drone_app_version": "0.1.40"}}).encode())
        with mock.patch.object(handlers_remote_admin, "_peer_proxy_request", return_value=ok) as proxy:
            handler = _handler(self.settings)
            handler._handle_admin_remote_connect({"peer_id": "peer-b", "username": "batocera", "password": "linux"})
        status, payload = handler.response
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "connected")
        self.assertEqual(payload["drone_app_version"], "0.1.40")
        # The verification call must hit system-info with Basic-auth for the
        # credentials just submitted, not this gateway's own.
        self.assertEqual(proxy.call_args.args[2], "/v1/api/admin/system-info")
        self.assertTrue(proxy.call_args.kwargs["authorization"].startswith("Basic "))
        self.assertIsNotNone(handlers_remote_admin._get_remote_session("peer-b"))

    def test_connect_wrong_credentials_is_401_and_not_cached(self) -> None:
        from app.web import handlers_remote_admin

        with mock.patch.object(handlers_remote_admin, "_peer_proxy_request", return_value=self._proxy_response(401)):
            handler = _handler(self.settings)
            handler._handle_admin_remote_connect({"peer_id": "peer-b", "username": "batocera", "password": "wrong"})
        self.assertEqual(handler.response[0], 401)
        self.assertIsNone(handlers_remote_admin._get_remote_session("peer-b"))

    def test_connect_admin_disabled_on_target_is_409(self) -> None:
        from app.web import handlers_remote_admin

        with mock.patch.object(handlers_remote_admin, "_peer_proxy_request", return_value=self._proxy_response(403)):
            handler = _handler(self.settings)
            handler._handle_admin_remote_connect({"peer_id": "peer-b", "username": "batocera", "password": "linux"})
        self.assertEqual(handler.response[0], 409)

    def test_connect_offline_peer_is_502(self) -> None:
        from app.web import handlers_remote_admin

        with mock.patch.object(handlers_remote_admin, "_peer_proxy_request", side_effect=URLError("unreachable")):
            handler = _handler(self.settings)
            handler._handle_admin_remote_connect({"peer_id": "peer-b", "username": "batocera", "password": "linux"})
        self.assertEqual(handler.response[0], 502)

    # -- disconnect --------------------------------------------------------

    def test_disconnect_clears_the_cached_session(self) -> None:
        from app.web import handlers_remote_admin

        handlers_remote_admin._cache_remote_session("peer-b", "Basic dGVzdA==", "0.1.40")
        handler = _handler(self.settings)
        handler._handle_admin_remote_disconnect({"peer_id": "peer-b"})
        self.assertEqual(handler.response[0], 200)
        self.assertIsNone(handlers_remote_admin._get_remote_session("peer-b"))

    # -- generic proxy -------------------------------------------------

    def test_proxy_without_a_session_is_401_not_connected(self) -> None:
        handler = _handler(self.settings)
        handler._handle_admin_remote_proxy("peer-b", "admin/system-info", "GET", "")
        status, payload = handler.response
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "not_connected")

    def test_proxy_unknown_peer_is_404(self) -> None:
        handler = _handler(self.settings)
        handler._handle_admin_remote_proxy("ghost", "admin/system-info", "GET", "")
        self.assertEqual(handler.response[0], 404)

    def test_proxy_decodes_a_url_encoded_mac_style_peer_id(self) -> None:
        # Regression: drone ids look like MAC addresses ("58:47:ca:7e:38:57").
        # The frontend encodeURIComponent()s the peer_id into the URL path
        # (`/remote/58%3A47%3Aca%3A7e%3A38%3A57/admin/system-info`); unlike
        # query-string values, raw path segments are never auto-decoded by the
        # api_routes.py dispatcher, so without unquoting here the paired-peer
        # lookup never matches and *every* proxied call 404s as "not a paired
        # Drone" -- even though connect (whose peer_id comes from the JSON
        # body, never the path) worked fine moments earlier.
        from urllib.parse import quote as urlquote

        from app.transfer import local_network
        from app.web import handlers_remote_admin

        mac_peer_id = "58:47:ca:7e:38:57"
        local_network.save_paired_peer(self.settings, {**PEER, "drone_id": mac_peer_id})
        handlers_remote_admin._cache_remote_session(mac_peer_id, "Basic dGVzdA==", "0.1.40")
        ok = self._proxy_response(200, b'{"fields": {"machine_id": "abc"}}')
        encoded_peer_id = urlquote(mac_peer_id, safe="")
        self.assertIn("%3A", encoded_peer_id)  # sanity: the colon really is encoded
        with mock.patch.object(handlers_remote_admin, "_peer_proxy_request", return_value=ok) as proxy:
            handler = _handler(self.settings)
            # Exactly what api_routes.py hands the method: the still-encoded
            # path segment, never decoded ahead of time.
            handler._handle_admin_remote_proxy(encoded_peer_id, "admin/system-info", "GET", "")
        self.assertEqual(handler.relayed["status"], 200)
        self.assertEqual(proxy.call_args.args[2], "/v1/api/admin/system-info")

    def test_proxy_rejects_a_sub_path_that_does_not_start_with_admin(self) -> None:
        # Regression: sub_path is the caller's *original* url ("admin/system-info",
        # since every page already calls api("/admin/...")) -- the handler must
        # not re-prepend "admin/" on top of that (that produced
        # "/v1/api/admin/admin/system-info", a 404 on the peer for every single
        # action). Guarding on the prefix also keeps this Basic-Auth proxy from
        # ever reaching the peer's separate mTLS-only /peer/* surface.
        from app.web import handlers_remote_admin

        handlers_remote_admin._cache_remote_session("peer-b", "Basic dGVzdA==", "0.1.40")
        handler = _handler(self.settings)
        handler._handle_admin_remote_proxy("peer-b", "peer/inventory/summary", "GET", "")
        status, payload = handler.response
        self.assertEqual(status, 400)
        self.assertIn("/admin/*", payload["error"])

    def test_proxy_relays_a_successful_get_verbatim(self) -> None:
        from app.web import handlers_remote_admin

        handlers_remote_admin._cache_remote_session("peer-b", "Basic dGVzdA==", "0.1.40")
        ok = self._proxy_response(200, b'{"fields": {"machine_id": "abc"}}')
        with mock.patch.object(handlers_remote_admin, "_peer_proxy_request", return_value=ok) as proxy:
            handler = _handler(self.settings)
            handler._handle_admin_remote_proxy("peer-b", "admin/system-info", "GET", "speed=1")
        self.assertEqual(handler.relayed["status"], 200)
        self.assertEqual(handler.relayed["headers"]["Content-Type"], "application/json")
        handler.wfile.write.assert_called_once_with(b'{"fields": {"machine_id": "abc"}}')
        self.assertEqual(proxy.call_args.args[2], "/v1/api/admin/system-info?speed=1")
        self.assertEqual(proxy.call_args.kwargs["authorization"], "Basic dGVzdA==")

    def test_proxy_forwards_the_post_body_and_content_type(self) -> None:
        from app.web import handlers_remote_admin

        handlers_remote_admin._cache_remote_session("peer-b", "Basic dGVzdA==", "0.1.40")
        ok = self._proxy_response(200, b'{"status": "queued"}')
        with mock.patch.object(handlers_remote_admin, "_peer_proxy_request", return_value=ok) as proxy:
            handler = _handler(
                self.settings,
                headers={"Content-Length": "16", "Content-Type": "application/json"},
                body=b'{"level": 55}\n  ',
            )
            handler._handle_admin_remote_proxy("peer-b", "admin/system-info/volume", "POST", "")
        self.assertEqual(proxy.call_args.kwargs["body"], b'{"level": 55}\n  ')
        self.assertEqual(proxy.call_args.kwargs["content_type"], "application/json")
        self.assertEqual(proxy.call_args.args[1], "POST")

    def test_proxy_401_from_target_clears_session_and_reports_clearly(self) -> None:
        from app.web import handlers_remote_admin

        handlers_remote_admin._cache_remote_session("peer-b", "Basic dGVzdA==", "0.1.40")
        with mock.patch.object(handlers_remote_admin, "_peer_proxy_request", return_value=self._proxy_response(401)):
            handler = _handler(self.settings)
            handler._handle_admin_remote_proxy("peer-b", "admin/system-info", "GET", "")
        self.assertEqual(handler.response[0], 401)
        self.assertIsNone(handlers_remote_admin._get_remote_session("peer-b"))
        # A follow-up call now reports not_connected rather than a stale rejection.
        handler2 = _handler(self.settings)
        handler2._handle_admin_remote_proxy("peer-b", "admin/system-info", "GET", "")
        self.assertEqual(handler2.response[1]["error"], "not_connected")

    def test_proxy_404_from_target_is_reported_as_version_mismatch(self) -> None:
        from app.web import handlers_remote_admin

        handlers_remote_admin._cache_remote_session("peer-b", "Basic dGVzdA==", "0.1.10")
        with mock.patch.object(handlers_remote_admin, "_peer_proxy_request", return_value=self._proxy_response(404)), \
                mock.patch.object(handlers_remote_admin, "_drone_app_version", return_value="0.1.53"):
            handler = _handler(self.settings)
            handler._handle_admin_remote_proxy("peer-b", "admin/some-new-route", "GET", "")
        status, payload = handler.response
        self.assertEqual(status, 404)
        self.assertIn("version mismatch", payload["error"])
        self.assertIn("0.1.53", payload["error"])
        self.assertIn("0.1.10", payload["error"])

    def test_proxy_connection_failure_is_502(self) -> None:
        from app.web import handlers_remote_admin

        handlers_remote_admin._cache_remote_session("peer-b", "Basic dGVzdA==", "0.1.40")
        with mock.patch.object(handlers_remote_admin, "_peer_proxy_request", side_effect=URLError("unreachable")):
            handler = _handler(self.settings)
            handler._handle_admin_remote_proxy("peer-b", "admin/system-info", "GET", "")
        self.assertEqual(handler.response[0], 502)

    def test_session_ttl_expiry_requires_reconnect(self) -> None:
        from app.web import handlers_remote_admin

        handlers_remote_admin._cache_remote_session("peer-b", "Basic dGVzdA==", "0.1.40")
        handlers_remote_admin._REMOTE_SESSIONS["peer-b"]["cached_at"] -= handlers_remote_admin.REMOTE_SESSION_TTL_SECONDS + 1
        handler = _handler(self.settings)
        handler._handle_admin_remote_proxy("peer-b", "admin/system-info", "GET", "")
        self.assertEqual(handler.response[1]["error"], "not_connected")


if __name__ == "__main__":
    unittest.main()
