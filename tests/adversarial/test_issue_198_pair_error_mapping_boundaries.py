"""Issue #198 boundaries: peer handshake failures stay 502 through real HTTP.

The worker unit tests mock ``_local_pair_peer`` and call the helper directly.
These cases drive ``POST /v1/api/admin/local-network/pair-by-address`` and the
discovered-peer pair route against a live Drone plus a scripted peer, covering
malformed peer bodies, empty/wrong codes, dropped connections, and auth.

Expected (issue + pair-by-address invariants):

* Peer HTTP errors on POST /v1/api/peer/pair are upstream failures: 502 JSON,
  never an unhandled 500 / urllib ``HTTP Error 403: Forbidden``.
* Failed attempts do not persist a paired-peer row.
* Fetch failures, self-collision, and unparseable addresses keep their existing
  502 / 409 / 400 mapping. Unauthenticated callers stay 401/403.
* The discovered-peer pair route uses the same handshake, so a peer 403 there
  is also 502, not 500.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import URLError
from urllib.parse import quote

from app.mock_data import seed_mock_userdata
from app.storage.state_store import database_path, load_payload
from app.transfer import local_network
from app.transfer.peer_workers import stop_local_network_workers
from app.web import handlers_network


PEER_ID = "bb:bb:bb:bb:01:98"
OPERATOR_ID = "aa:aa:aa:aa:01:98"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakePeerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler API
        return

    def _write(self, code: int, body, content_type: str) -> None:
        raw = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/v1/api/peer/info":
            self._write(self.server.info_status, self.server.info_body, self.server.info_content_type)
            return
        self._write(404, {"error": "not found"}, "application/json")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.server.last_pair_body = self.rfile.read(length) if length else b""
        self.server.pair_hits += 1
        if self.server.pair_mode == "drop":
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return
        if self.path.split("?", 1)[0] == "/v1/api/peer/pair":
            self._write(self.server.pair_status, self.server.pair_body, self.server.pair_content_type)
            return
        self._write(404, {"error": "not found"}, "application/json")


class FakePeer:
    def __init__(self, drone_id: str = PEER_ID) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePeerHandler)
        self.server.drone_id = drone_id
        self.server.info_status = 200
        self.server.info_content_type = "application/json"
        self.server.info_body = {
            "service": local_network.DISCOVERY_SERVICE,
            "drone_id": drone_id,
            "name": "scripted-peer",
            "hostname": "scripted-peer",
            "scheme": "http",
            "api_port": int(self.server.server_address[1]),
            "peer_mtls_port": int(self.server.server_address[1]),
            "reachable_url": self.url,
            "certificate_fingerprint": "ab" * 32,
        }
        self.server.pair_status = 403
        self.server.pair_content_type = "application/json"
        self.server.pair_body = {"error": "invalid or expired pairing code"}
        self.server.pair_mode = "reply"
        self.server.pair_hits = 0
        self.server.last_pair_body = b""
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def reject_json(self, code: int, body) -> None:
        self.server.pair_mode = "reply"
        self.server.pair_status = code
        self.server.pair_content_type = "application/json"
        self.server.pair_body = body

    def reject_raw(self, code: int, body: bytes, content_type: str) -> None:
        self.server.pair_mode = "reply"
        self.server.pair_status = code
        self.server.pair_content_type = content_type
        self.server.pair_body = body

    def drop_pair(self) -> None:
        self.server.pair_mode = "drop"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class PairErrorMappingBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.drone_api import Settings, create_server

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.userdata = base / "userdata"
        seed_mock_userdata(self.userdata)
        env = {
            "USERDATA_ROOT": str(self.userdata),
            "ROMS_ROOT": str(self.userdata / "roms"),
            "BIOS_ROOT": str(self.userdata / "bios"),
            "SAVES_ROOT": str(self.userdata / "saves"),
            "THEMES_ROOT": str(self.userdata / "themes"),
            "BATOCERA_CONF_FILE": str(self.userdata / "system" / "batocera.conf"),
            "ES_SETTINGS_FILE": str(
                self.userdata / "system" / "configs" / "emulationstation" / "es_settings.cfg"
            ),
            "DRONE_APP_USERNAME": "admin",
            "DRONE_APP_PASSWORD": "changeme",
            "HTTPS_PORT": "0",
            "HTTP_ONLY": "1",
            "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1",
            "LOG_DIR": str(base / "logs"),
            "ROM_METADATA_POLL_SECONDS": "0",
            "DRONE_AUTO_UPDATE_POLL_SECONDS": "0",
            "DRONE_CAST_ENABLED": "0",
            "DRONE_DEVICE_ID": OPERATOR_ID,
        }
        env_patch = mock.patch.dict(os.environ, env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.settings = Settings.from_env()
        try:
            self.drone = create_server(self.settings)
        except PermissionError as error:
            self.skipTest(f"Socket bind is not allowed in this environment: {error}")
        self.port = int(self.drone.server_address[1])
        thread = threading.Thread(target=self.drone.serve_forever, daemon=True)
        thread.start()

        def stop_drone() -> None:
            self.drone.shutdown()
            self.drone.server_close()
            thread.join(timeout=3)
            stop_local_network_workers()

        self.addCleanup(stop_drone)
        self.cookie = self._login()
        self.peer = FakePeer()
        self.addCleanup(self.peer.stop)

    def _login(self) -> str:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/api/auth/login",
            data=json.dumps({"username": "admin", "password": "changeme"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            jar = SimpleCookie()
            jar.load(response.headers.get("Set-Cookie"))
        morsel = next(iter(jar.values()))
        return f"{morsel.key}={morsel.value}"

    def _request(self, method: str, path: str, body=None, *, auth: bool = True, timeout: float = 15):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Cookie"] = self.cookie
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {"raw": raw.decode("utf-8", "replace")}
            return error.code, parsed

    def _pair_by_address(self, pairing_code="not-a-code", address=None, **extra):
        payload = {"address": address or self.peer.url, "pairing_code": pairing_code}
        payload.update(extra)
        return self._request("POST", "/v1/api/admin/local-network/pair-by-address", payload)

    def _paired_ids(self) -> set[str]:
        payload = load_payload(database_path(self.userdata), "local_paired_peers", {})
        if not isinstance(payload, dict):
            return set()
        return set(payload)

    def _assert_upstream_reject(self, status: int, payload: dict, *, peer_http: int, detail: str = "") -> None:
        error = str(payload.get("error") or "")
        self.assertEqual(status, 502, payload)
        self.assertNotEqual(status, 500, payload)
        self.assertIsInstance(payload.get("error"), str)
        self.assertNotEqual(error.strip(), f"HTTP Error {peer_http}: Forbidden")
        self.assertNotIn("Traceback", error)
        self.assertIn(str(peer_http), error)
        if detail:
            self.assertIn(detail, error)
        self.assertLess(len(error), 1000)
        self.assertNotIn(PEER_ID, self._paired_ids())

    def test_peer_403_json_is_502_with_peer_text_and_no_pairing_row(self) -> None:
        status, payload = self._pair_by_address("not-a-code")
        self._assert_upstream_reject(
            status, payload, peer_http=403, detail="invalid or expired pairing code"
        )
        self.assertGreaterEqual(self.peer.server.pair_hits, 1)

    def test_empty_and_missing_and_whitespace_codes_are_peer_rejects_not_500(self) -> None:
        for body in (
            {"address": self.peer.url, "pairing_code": ""},
            {"address": self.peer.url},
            {"address": self.peer.url, "pairing_code": "   "},
            {"address": self.peer.url, "pairing_code": ["not-a-code"]},
        ):
            with self.subTest(body=body):
                status, payload = self._request(
                    "POST", "/v1/api/admin/local-network/pair-by-address", body
                )
                self._assert_upstream_reject(
                    status, payload, peer_http=403, detail="invalid or expired pairing code"
                )

    def test_peer_403_html_and_empty_and_array_bodies_still_map_to_502(self) -> None:
        self.peer.reject_raw(403, b"<html>nope</html>", "text/html")
        status, payload = self._pair_by_address()
        self._assert_upstream_reject(status, payload, peer_http=403)

        self.peer.reject_raw(403, b"", "application/json")
        status, payload = self._pair_by_address()
        self._assert_upstream_reject(status, payload, peer_http=403)

        self.peer.reject_json(403, ["invalid or expired pairing code"])
        status, payload = self._pair_by_address()
        self._assert_upstream_reject(status, payload, peer_http=403)

        self.peer.reject_json(403, {"error": {"code": "invalid"}})
        status, payload = self._pair_by_address()
        self._assert_upstream_reject(status, payload, peer_http=403)

    def test_long_peer_error_is_truncated_json_not_500(self) -> None:
        self.peer.reject_json(403, {"error": "invalid or expired pairing code " + ("X" * 800)})
        status, payload = self._pair_by_address()
        self._assert_upstream_reject(
            status, payload, peer_http=403, detail="invalid or expired pairing code"
        )

    def test_peer_500_and_409_are_upstream_502_not_local_500(self) -> None:
        self.peer.reject_json(500, {"error": "peer exploded"})
        status, payload = self._pair_by_address()
        self._assert_upstream_reject(status, payload, peer_http=500, detail="peer exploded")

        self.peer.reject_json(409, {"error": "duplicate Drone identity detected"})
        status, payload = self._pair_by_address()
        self._assert_upstream_reject(
            status, payload, peer_http=409, detail="duplicate Drone identity detected"
        )

    def test_pair_connection_drop_after_info_is_502_not_500(self) -> None:
        self.peer.drop_pair()
        status, payload = self._pair_by_address()
        error = str(payload.get("error") or "")
        self.assertEqual(status, 502, payload)
        self.assertNotEqual(status, 500, payload)
        self.assertTrue(error)
        self.assertNotIn(PEER_ID, self._paired_ids())

    def test_existing_address_and_auth_mappings_stay_put(self) -> None:
        status, payload = self._pair_by_address("XXXXXXXX", address=f"http://127.0.0.1:{self.port}")
        self.assertEqual(status, 409, payload)
        self.assertIn("itself", str(payload.get("error") or "").lower())

        status, payload = self._pair_by_address("XXXXXXXX", address="ftp://nope")
        self.assertEqual(status, 400, payload)

        status, payload = self._pair_by_address("XXXXXXXX", address="http://127.0.0.1:9")
        self.assertEqual(status, 502, payload)

        status, payload = self._request(
            "POST",
            "/v1/api/admin/local-network/pair-by-address",
            {"address": self.peer.url, "pairing_code": "not-a-code"},
            auth=False,
        )
        # Loopback callers are pre-authenticated; the wrong code is still a
        # peer rejection (502), never an unhandled 500.
        self.assertEqual(status, 502, payload)
        self.assertNotEqual(status, 500, payload)

    def test_malformed_json_body_is_client_error_not_500(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/api/admin/local-network/pair-by-address",
            data=b"{not-json",
            method="POST",
            headers={"Cookie": self.cookie, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            raw = error.read()
        self.assertNotEqual(status, 500, raw)
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)

    def test_discovered_peer_pair_route_maps_peer_403_to_502(self) -> None:
        recorded = local_network.record_discovered_peer(
            self.settings,
            {
                "service": local_network.DISCOVERY_SERVICE,
                "drone_id": PEER_ID,
                "name": "scripted-peer",
                "hostname": "scripted-peer",
                "scheme": "http",
                "api_port": self.peer.port,
                "peer_mtls_port": self.peer.port,
                "reachable_url": self.peer.url,
                "certificate_fingerprint": "ab" * 32,
            },
            source_ip="127.0.0.1",
        )
        self.assertIsNotNone(recorded)
        status, payload = self._request(
            "POST",
            f"/v1/api/admin/local-network/peers/{quote(PEER_ID, safe='')}/pair",
            {"pairing_code": "not-a-code"},
        )
        self._assert_upstream_reject(
            status, payload, peer_http=403, detail="invalid or expired pairing code"
        )

    def test_urlerror_from_helper_is_502_reachability_not_500(self) -> None:
        handler = handlers_network.HandlersNetworkMixin()
        handler.settings = mock.MagicMock(use_fake_data=False)
        handler._send_json = mock.Mock()
        with mock.patch.object(
            handlers_network, "_local_pair_peer", side_effect=URLError("timed out")
        ):
            result = handler._pair_peer_or_reply({"drone_id": PEER_ID, "reachable_url": self.peer.url}, "x")
        self.assertIsNone(result)
        status, payload = handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("Could not reach that drone", payload["error"])


if __name__ == "__main__":
    unittest.main()
