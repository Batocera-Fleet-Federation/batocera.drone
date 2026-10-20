"""Issue #224 UAT: pair-by-address must not 500 when the peer answers HTTP 200 without status=paired.

Reproduction from the issue: GET /v1/api/peer/info succeeds, then POST /v1/api/peer/pair
returns HTTP 200 JSON whose status is not paired. Before the fix, _local_pair_peer raised
RuntimeError('peer did not accept pairing request') and _pair_peer_or_reply only mapped
HTTPError/URLError/OSError, so do_POST logged a 500 internal error.

Expected, derived from that report and the pair-by-address contract (#198/#223):

* A peer that answers 200 with a body that is not an accepted pairing is an upstream
  rejection, not a local bug. The admin endpoint must answer 502 JSON, never 500.
* The operator-facing body is JSON with an error string. It does not include a traceback,
  urllib's raw HTTP Error text, or the peer's own 200 payload (that payload is not an
  HTTP error document; it may be a proxy page or application JSON).
* A rejected attempt stores no paired-peer row. A later peer HTTP 403 still maps to 502.
* Pointing the same endpoint at this Drone remains 409; an unparseable address remains 400.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.mock_data import seed_mock_userdata
from app.storage.state_store import database_path, load_payload
from app.transfer import local_network
from app.transfer.peer_workers import stop_local_network_workers


PEER_ID = "bb:bb:bb:bb:02:24"
OPERATOR_ID = "aa:aa:aa:aa:02:24"
MARKER = "ISSUE224-PEER-BODY-MUST-NOT-LEAK"


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
            self.server.info_hits += 1
            self._write(self.server.info_status, self.server.info_body, self.server.info_content_type)
            return
        self._write(404, {"error": "not found"}, "application/json")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.server.last_pair_body = self.rfile.read(length) if length else b""
        self.server.pair_hits += 1
        if self.path.split("?", 1)[0] == "/v1/api/peer/pair":
            self._write(self.server.pair_status, self.server.pair_body, self.server.pair_content_type)
            return
        self._write(404, {"error": "not found"}, "application/json")


class FakePeer:
    def __init__(self, drone_id: str = PEER_ID) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePeerHandler)
        self.server.info_status = 200
        self.server.info_content_type = "application/json"
        self.server.info_body = {
            "service": local_network.DISCOVERY_SERVICE,
            "drone_id": drone_id,
            "name": "scripted-peer-224",
            "hostname": "scripted-peer-224",
            "scheme": "http",
            "api_port": int(self.server.server_address[1]),
            "peer_mtls_port": int(self.server.server_address[1]),
            "reachable_url": self.url,
            "certificate_fingerprint": "ab" * 32,
        }
        self.server.info_hits = 0
        self.server.pair_status = 200
        self.server.pair_content_type = "application/json"
        self.server.pair_body = {"status": "queued", "error": MARKER}
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

    def reply_pair(self, status: int, body, content_type: str = "application/json") -> None:
        self.server.pair_status = status
        self.server.pair_body = body
        self.server.pair_content_type = content_type

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class PairByAddressNonPaired200UAT(unittest.TestCase):
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
                return response.status, json.loads(raw) if raw else {}, raw
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {"raw": raw.decode("utf-8", "replace")}
            return error.code, parsed, raw

    def _pair_by_address(self, pairing_code="not-a-code", address=None):
        payload = {"address": address or self.peer.url, "pairing_code": pairing_code, "certificate_fingerprint": "ab" * 32}
        return self._request("POST", "/v1/api/admin/local-network/pair-by-address", payload)

    def _paired_ids(self) -> set[str]:
        payload = load_payload(database_path(self.userdata), "local_paired_peers", {})
        if isinstance(payload, dict):
            return set(payload)
        return set()

    def _assert_peer_200_is_gateway(self, status: int, payload: dict, raw: bytes, *forbidden: str) -> None:
        error = str(payload.get("error") or "")
        self.assertEqual(status, 502, payload)
        self.assertNotEqual(status, 500, payload)
        self.assertIsInstance(payload, dict)
        self.assertIsInstance(payload.get("error"), str)
        self.assertTrue(error.strip())
        self.assertNotIn("Traceback", error)
        self.assertNotIn("HTTP Error", error)
        self.assertLess(len(error), 1000)
        self.assertNotIn(PEER_ID, self._paired_ids())
        self.assertNotIn(MARKER.encode("utf-8"), raw)
        for token in forbidden:
            self.assertNotIn(token, error)
            self.assertNotIn(token.encode("utf-8"), raw)

    def test_peer_200_queued_status_is_502_not_unhandled_500(self) -> None:
        status, payload, raw = self._pair_by_address()
        self._assert_peer_200_is_gateway(status, payload, raw, MARKER, "queued")
        self.assertGreaterEqual(self.peer.server.info_hits, 1)
        self.assertGreaterEqual(self.peer.server.pair_hits, 1)
        self.assertIn(b"pairing_code", self.peer.server.last_pair_body)

    def test_peer_200_html_proxy_page_is_502_and_body_is_not_echoed(self) -> None:
        self.peer.reply_pair(200, b"<html>captive portal " + MARKER.encode() + b"</html>", "text/html")
        status, payload, raw = self._pair_by_address()
        self._assert_peer_200_is_gateway(status, payload, raw, "captive portal", "<html>")

    def test_existing_self_and_address_mappings_stay_put_after_a_rejected_200(self) -> None:
        status, payload, raw = self._pair_by_address()
        self._assert_peer_200_is_gateway(status, payload, raw, MARKER)

        status, payload, _ = self._pair_by_address("XXXXXXXX", address=f"http://127.0.0.1:{self.port}")
        self.assertEqual(status, 409, payload)
        self.assertIn("itself", str(payload.get("error") or "").lower())

        status, payload, _ = self._pair_by_address("XXXXXXXX", address="ftp://nope")
        self.assertEqual(status, 400, payload)

        self.peer.reply_pair(403, {"error": "invalid or expired pairing code"})
        status, payload, _ = self._pair_by_address()
        error = str(payload.get("error") or "")
        self.assertEqual(status, 502, payload)
        self.assertNotEqual(status, 500, payload)
        self.assertIn("403", error)
        self.assertIn("invalid or expired pairing code", error.lower())
        self.assertNotIn(PEER_ID, self._paired_ids())


if __name__ == "__main__":
    unittest.main()
