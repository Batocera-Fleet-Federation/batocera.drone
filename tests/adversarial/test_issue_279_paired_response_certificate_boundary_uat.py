"""Issue #279: pair-by-address rejects a paired answer without a certificate."""

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


OPERATOR_ID = "aa:aa:aa:aa:02:79"
PEER_ID = "bb:bb:bb:bb:02:79"


class _PeerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - required handler signature
        return

    def _reply(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/v1/api/peer/info":
            self._reply(200, self.server.info_body)
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.split("?", 1)[0] == "/v1/api/peer/pair":
            self.server.pair_hits += 1
            self._reply(200, self.server.pair_body)
            return
        self._reply(404, {"error": "not found"})


class _Peer:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _PeerHandler)
        self.server.info_body = {
            "service": local_network.DISCOVERY_SERVICE,
            "drone_id": PEER_ID,
            "name": "issue-279-peer",
            "scheme": "http",
            "api_port": self.server.server_address[1],
            "peer_mtls_port": self.server.server_address[1],
            "reachable_url": self.url,
            "certificate_fingerprint": "ab" * 32,
        }
        self.server.pair_body = {"status": "paired", "drone_id": PEER_ID}
        self.server.pair_hits = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class PairedResponseCertificateBoundaryUAT(unittest.TestCase):
    def setUp(self) -> None:
        from app.drone_api import Settings, create_server

        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name).resolve()
        self.userdata = root / "userdata"
        seed_mock_userdata(self.userdata)
        environment = {
            "USERDATA_ROOT": str(self.userdata),
            "ROMS_ROOT": str(self.userdata / "roms"),
            "BIOS_ROOT": str(self.userdata / "bios"),
            "SAVES_ROOT": str(self.userdata / "saves"),
            "THEMES_ROOT": str(self.userdata / "themes"),
            "BATOCERA_CONF_FILE": str(self.userdata / "system" / "batocera.conf"),
            "ES_SETTINGS_FILE": str(self.userdata / "system" / "configs" / "emulationstation" / "es_settings.cfg"),
            "DRONE_APP_USERNAME": "admin",
            "DRONE_APP_PASSWORD": "changeme",
            "HTTPS_PORT": "0",
            "HTTP_ONLY": "1",
            "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1",
            "LOG_DIR": str(root / "logs"),
            "ROM_METADATA_POLL_SECONDS": "0",
            "DRONE_AUTO_UPDATE_POLL_SECONDS": "0",
            "DRONE_CAST_ENABLED": "0",
            "DRONE_DEVICE_ID": OPERATOR_ID,
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.drone = create_server(Settings.from_env())
        self.port = self.drone.server_address[1]
        self.thread = threading.Thread(target=self.drone.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_drone)
        self.cookie = self._login()
        self.peer = _Peer()
        self.addCleanup(self.peer.stop)

    def _stop_drone(self) -> None:
        self.drone.shutdown()
        self.drone.server_close()
        self.thread.join(timeout=3)
        stop_local_network_workers()

    def _login(self) -> str:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/api/auth/login",
            data=json.dumps({"username": "admin", "password": "changeme"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            cookies = SimpleCookie()
            cookies.load(response.headers.get("Set-Cookie"))
        morsel = next(iter(cookies.values()))
        return f"{morsel.key}={morsel.value}"

    def _pair_by_address(self) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/api/admin/local-network/pair-by-address",
            data=json.dumps({"address": self.peer.url, "pairing_code": "operator-code"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Cookie": self.cookie},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_paired_matching_peer_without_certificate_is_a_502_and_is_not_saved(self) -> None:
        # A peer has accepted the code and claimed the expected identity.  Its missing
        # trust material is nevertheless a peer-protocol rejection, never caller input.
        cases = (
            ("absent", {}),
            ("null", {"certificate_pem": None}),
            ("empty", {"certificate_pem": ""}),
            ("not-a-pem", {"certificate_pem": "not a certificate"}),
            ("unparseable-pem", {"certificate_pem": "-----BEGIN CERTIFICATE-----\\ngarbage"}),
        )
        for label, certificate in cases:
            with self.subTest(case=label):
                self.peer.server.pair_body = {"status": "paired", "drone_id": PEER_ID}
                self.peer.server.pair_body.update(certificate)
                status, payload = self._pair_by_address()
                self.assertEqual(status, 502, payload)
                self.assertEqual(payload.get("error"), "That drone did not accept the pairing request.")
                stored = load_payload(database_path(self.userdata), "local_paired_peers", {})
                self.assertNotIn(PEER_ID, stored if isinstance(stored, dict) else {})

        self.assertEqual(self.peer.server.pair_hits, len(cases))


if __name__ == "__main__":
    unittest.main()
