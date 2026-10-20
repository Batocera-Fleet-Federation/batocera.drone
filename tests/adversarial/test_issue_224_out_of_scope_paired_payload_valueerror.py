"""Adversarial finding for issue #224, now the expected 502 mapping (issue #279).

Issue #224 is scoped to pair-by-address answering 500 when POST /v1/api/peer/pair returns
HTTP 200 whose status is not paired. A related class of peer 200 escaped the pairing helper:
status=paired and a matching drone_id, but no certificate_pem. Before #279 the unpinned path
called _save_local_peer_certificate, which raised ValueError('peer certificate is required'),
and do_POST mapped that to HTTP 400.

Adjudicated in #279: a peer's paired-looking answer without a certificate is a rejected pairing,
the same peer-protocol 502 as the other malformed 200 answers. The 400 this suite originally
pinned was the defect being reported, so the assertions now expect 502 and no persisted pin.
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


class FakePeerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler API
        return

    def _write(self, code: int, body) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/v1/api/peer/info":
            self._write(200, self.server.info_body)
            return
        self._write(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.pair_hits += 1
        if self.path.split("?", 1)[0] == "/v1/api/peer/pair":
            self._write(200, self.server.pair_body)
            return
        self._write(404, {"error": "not found"})


class FakePeer:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakePeerHandler)
        self.server.info_body = {
            "service": local_network.DISCOVERY_SERVICE,
            "drone_id": PEER_ID,
            "name": "scripted-peer-224",
            "scheme": "http",
            "api_port": int(self.server.server_address[1]),
            "peer_mtls_port": int(self.server.server_address[1]),
            "reachable_url": self.url,
            "certificate_fingerprint": "ab" * 32,
        }
        self.server.pair_body = {"status": "paired", "drone_id": PEER_ID}
        self.server.pair_hits = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{int(self.server.server_address[1])}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class PairedPayloadMissingCertTests(unittest.TestCase):
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

    def test_paired_200_without_certificate_is_502_not_400(self) -> None:
        """Expected (#279): a paired answer without a certificate is a peer rejection, 502."""
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/api/admin/local-network/pair-by-address",
            data=json.dumps({"address": self.peer.url, "pairing_code": "not-a-code", "certificate_fingerprint": "ab" * 32}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Cookie": self.cookie},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            raw = error.read()
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"raw": raw.decode("utf-8", "replace")}

        self.assertEqual(status, 502, payload)
        self.assertNotEqual(status, 400, payload)
        self.assertNotEqual(status, 500, payload)
        self.assertEqual(payload.get("error"), "That drone did not accept the pairing request.")
        self.assertGreaterEqual(self.peer.server.pair_hits, 1)
        stored = load_payload(database_path(self.userdata), "local_paired_peers", {})
        self.assertNotIn(PEER_ID, stored if isinstance(stored, dict) else {})


if __name__ == "__main__":
    unittest.main()
