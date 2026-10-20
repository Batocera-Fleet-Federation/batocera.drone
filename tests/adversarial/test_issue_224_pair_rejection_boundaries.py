"""Issue #224 boundaries: malformed 200 pairing answers stay 502 through the real HTTP path.

The worker unit tests mock _local_pair_peer or a single non-JSON urlopen. These cases drive
POST /v1/api/admin/local-network/pair-by-address against a live Drone plus a scripted peer,
and the shared _pair_peer_or_reply helper, covering empty/non-object/wrong-status bodies,
identity mismatch, and the #198/#223 mappings that must not regress.

Expected (issue + pair-by-address invariants):

* HTTP 200 whose JSON status is not paired, whose body is not a JSON object, or whose
  declared drone_id does not match the discovered peer is the peer's answer: 502 JSON,
  never an unhandled 500. The peer's 200 text is not copied into the operator error.
* Failed attempts do not persist a paired-peer row.
* Peer HTTP 403 remains 502 with the peer's error text (#198). A local identity hold
  remains 503, not unreachable (#223). Successful pairing still returns the paired dict
  from the helper without sending an error.
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
from urllib.error import HTTPError, URLError

from app.mock_data import seed_mock_userdata
from app.storage.state_store import database_path, load_payload
from app.transfer import local_network
from app.transfer.peer_connectivity import PeerIdentityHold
from app.transfer.peer_workers import stop_local_network_workers
from app.web import handlers_network


PEER_ID = "bb:bb:bb:bb:02:24"
OPERATOR_ID = "aa:aa:aa:aa:02:24"
FINGERPRINT = "ab" * 32
HOLD_MESSAGE = "peer pairing is held pending Drone identity verification"
LEAK = "issue-224-must-not-echo"


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
            "certificate_fingerprint": FINGERPRINT,
        }
        self.server.pair_status = 200
        self.server.pair_content_type = "application/json"
        self.server.pair_body = {"status": "queued"}
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


class PairRejectionBoundaryTests(unittest.TestCase):
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

    def _request(self, method: str, path: str, body=None, timeout: float = 15):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json", "Cookie": self.cookie},
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

    def _pair_by_address(self, pairing_code="not-a-code"):
        return self._request(
            "POST",
            "/v1/api/admin/local-network/pair-by-address",
            {"address": self.peer.url, "pairing_code": pairing_code, "certificate_fingerprint": "ab" * 32},
        )

    def _paired_ids(self) -> set[str]:
        payload = load_payload(database_path(self.userdata), "local_paired_peers", {})
        if isinstance(payload, dict):
            return set(payload)
        return set()

    def _assert_gateway(self, status: int, payload: dict, raw: bytes, *forbidden: str) -> None:
        error = str(payload.get("error") or "")
        self.assertEqual(status, 502, payload)
        self.assertNotEqual(status, 500, payload)
        self.assertIsInstance(payload.get("error"), str)
        self.assertTrue(error.strip())
        self.assertNotIn("Traceback", error)
        self.assertNotIn("HTTP Error 200", error)
        self.assertLess(len(error), 1000)
        self.assertNotIn(PEER_ID, self._paired_ids())
        for token in forbidden:
            self.assertNotIn(token, error)
            self.assertNotIn(token.encode("utf-8"), raw)

    def test_malformed_200_bodies_are_502_not_500(self) -> None:
        cases = (
            ({"status": "queued", "error": LEAK}, "application/json", (LEAK, "queued")),
            ({"status": "rejected", "error": LEAK}, "application/json", (LEAK,)),
            ({"status": "Paired", "error": LEAK}, "application/json", (LEAK, "Paired")),
            ({"status": "paired ", "error": LEAK}, "application/json", (LEAK,)),
            ({"status": None, "error": LEAK}, "application/json", (LEAK,)),
            ({"error": LEAK}, "application/json", (LEAK,)),
            ({}, "application/json", ()),
            (["paired", LEAK], "application/json", (LEAK,)),
            (b"", "application/json", ()),
            (b"null", "application/json", ()),
            (b"true", "application/json", ()),
            (b"{not-json " + LEAK.encode() + b"}", "application/json", (LEAK,)),
            (b"\xff\xfe" + LEAK.encode("utf-16", "replace"), "application/octet-stream", ()),
        )
        for body, content_type, forbidden in cases:
            with self.subTest(body=body, content_type=content_type):
                self.peer.reply_pair(200, body, content_type)
                status, payload, raw = self._pair_by_address()
                self._assert_gateway(status, payload, raw, *forbidden)
                self.assertGreaterEqual(self.peer.server.pair_hits, 1)

    def test_paired_status_with_wrong_drone_id_is_502_not_500(self) -> None:
        self.peer.reply_pair(
            200,
            {"status": "paired", "drone_id": "cc:cc:cc:cc:99:99", "error": LEAK, "name": LEAK},
        )
        status, payload, raw = self._pair_by_address()
        self._assert_gateway(status, payload, raw, LEAK, "cc:cc:cc:cc:99:99")

    def test_peer_403_still_maps_to_502_with_peer_text(self) -> None:
        self.peer.reply_pair(403, {"error": "invalid or expired pairing code"})
        status, payload, _ = self._pair_by_address()
        error = str(payload.get("error") or "")
        self.assertEqual(status, 502, payload)
        self.assertIn("403", error)
        self.assertIn("invalid or expired pairing code", error.lower())
        self.assertNotIn(PEER_ID, self._paired_ids())

    def test_pair_by_address_documents_502(self) -> None:
        from app.drone_api import OPENAPI_SPEC

        responses = OPENAPI_SPEC["paths"]["/admin/local-network/pair-by-address"]["post"]["responses"]
        self.assertIn("502", responses)
        self.assertIn("500", responses)


class PairHelperMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler.settings.device_id = "drone-self"
        self.handler._send_json = mock.Mock()
        self.peer = {"drone_id": PEER_ID, "reachable_url": "http://peer.example:8080"}

    def test_helper_does_not_let_a_non_paired_200_escape(self) -> None:
        from app.transfer.peer_connectivity import PeerPairingRejected

        rejected = PeerPairingRejected("peer did not accept pairing request")
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=rejected):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"], "That drone did not accept the pairing request.")

    def test_identity_hold_is_still_503_not_a_peer_rejection(self) -> None:
        hold = PeerIdentityHold(HOLD_MESSAGE)
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=hold):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], HOLD_MESSAGE)
        self.assertNotIn("reach", payload["error"].lower())
        self.assertNotIn("did not accept", payload["error"].lower())

    def test_unreachable_peer_is_still_502_reachability(self) -> None:
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=URLError("timed out")):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("Could not reach that drone", payload["error"])

    def test_peer_http_error_is_still_502_with_status(self) -> None:
        error = HTTPError(
            url="http://peer.example/v1/api/peer/pair",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=__import__("io").BytesIO(json.dumps({"error": "invalid or expired pairing code"}).encode()),
        )
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=error):
            self.assertIsNone(self.handler._pair_peer_or_reply(self.peer, "x", FINGERPRINT))
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("HTTP 403", payload["error"])
        self.assertIn("invalid or expired pairing code", payload["error"])

    def test_successful_pairing_still_returns_without_an_error_reply(self) -> None:
        paired = {"drone_id": PEER_ID, "certificate_path": ""}
        with mock.patch.object(handlers_network, "_local_pair_peer", return_value=paired):
            self.assertEqual(self.handler._pair_peer_or_reply(self.peer, "CODE", FINGERPRINT), paired)
        self.handler._send_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
