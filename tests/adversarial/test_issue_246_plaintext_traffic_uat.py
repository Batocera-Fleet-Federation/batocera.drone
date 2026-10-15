"""Issue #246 UAT: a live plaintext listener must not see TLS-paired peer traffic.

The issue's attack is an on-LAN spoofed announce (service=DISCOVERY_SERVICE,
scheme=http, attacker source IP) that makes the next health check or
inventory/download dial ``http://attacker``. These cases bind a real HTTP
server, persist a TLS-paired pin, inject the spoof through
``record_discovered_peer``, then drive the production client helpers with
``urlopen`` unmocked. The listener's hit log is the oracle.

Servers bind loopback only. No discovery worker is started.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity, peer_download

FINGERPRINT = "aa" * 32
PEER_ID = "victim-246"
PAIR_CODE = "24680123"


def _settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "DRONE_DEVICE_ID": "local-246",
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "HTTP_ONLY": "0",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class _PlaintextPeer(BaseHTTPRequestHandler):
    hits: list

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.hits.append(
            {
                "path": self.path,
                "method": self.command,
                "body": body,
            }
        )

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        payload = json.dumps({"status": "ok", "files": []}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        self._record()
        payload = json.dumps({"status": "paired", "drone_id": PEER_ID}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class Issue246PlaintextTrafficUAT(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hits: list = []
        _PlaintextPeer.hits = cls.hits
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _PlaintextPeer)
        cls.port = int(cls.server.server_address[1])
        cls.thread = threading.Thread(target=cls.server.serve_forever, name="issue-246-http", daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.hits.clear()
        temp = tempfile.TemporaryDirectory(prefix="issue-246-uat-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = _settings(self.root)
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)
        self._identity = mock.patch.object(
            peer_connectivity, "_identity_swarm_networking_allowed", return_value=True
        )
        self._identity.start()
        self.addCleanup(self._identity.stop)

    def _pin_tls(self) -> dict:
        return local_network.save_paired_peer(
            self.settings,
            {
                "drone_id": PEER_ID,
                "name": "Victim",
                "reachable_url": "https://127.0.0.1:1",
                "advertised_reachable_url": "https://victim.local:7443",
                "scheme": "https",
                "api_port": 7443,
                "peer_mtls_port": 8443,
                "certificate_fingerprint": FINGERPRINT,
                "paired_over_tls": True,
            },
        )

    def _spoof(self) -> dict:
        return local_network.record_discovered_peer(
            self.settings,
            {
                "service": local_network.DISCOVERY_SERVICE,
                "drone_id": PEER_ID,
                "scheme": "http",
                "api_port": self.port,
                "peer_mtls_port": self.port,
                "reachable_url": f"http://127.0.0.1:{self.port}",
                "hostname": "attacker.local",
                "certificate_fingerprint": "bb" * 32,
                "paired_over_tls": False,
            },
            "127.0.0.1",
        )

    def test_health_and_inventory_do_not_hit_spoofed_http_listener(self) -> None:
        self._pin_tls()
        discovered = self._spoof()
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        self.assertEqual(stored["reachable_url"], "https://127.0.0.1:1")
        self.assertTrue(stored.get("paired_over_tls"))
        self.assertTrue(str(discovered.get("reachable_url") or "").startswith("https://"))

        for peer in (stored, discovered):
            with self.assertRaises(Exception):
                peer_connectivity._peer_get_json_for_peer(
                    peer, "/v1/api/peer/roms", self.settings, peer_id=PEER_ID, timeout=0.5
                )
            with self.assertRaises(Exception):
                peer_connectivity._peer_post_json_for_peer(
                    peer,
                    "/v1/api/peer/health",
                    {"probe": True},
                    self.settings,
                    peer_id=PEER_ID,
                    timeout=0.5,
                )

        preferred = peer_connectivity._preferred_peer_address(
            stored, settings=self.settings, peer_id=PEER_ID
        )
        self.assertIsNotNone(preferred)
        self.assertTrue(str(preferred).lower().startswith("https://"), preferred)
        self.assertFalse(any(hit["path"] for hit in self.hits), self.hits)

    def test_download_helper_does_not_fetch_rom_bytes_over_plaintext(self) -> None:
        self._pin_tls()
        self._spoof()
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        (self.settings.roms_root / "nes").mkdir(parents=True, exist_ok=True)
        with self.assertRaises(Exception):
            peer_download._download_rom_from_peer(
                self.settings,
                {},
                stored,
                "nes",
                "attack.nes",
            )
        self.assertFalse(self.hits, self.hits)
        leftover = self.settings.roms_root / "nes" / "attack.nes"
        self.assertFalse(leftover.exists())

    def test_repair_does_not_post_pairing_code_to_plaintext_listener(self) -> None:
        self._pin_tls()
        self._spoof()
        attacker_peer = {
            "drone_id": PEER_ID,
            "scheme": "http",
            "reachable_url": f"http://127.0.0.1:{self.port}",
        }
        with mock.patch.object(peer_connectivity, "DroneCertificateManager") as manager:
            manager.return_value.ensure_certificate.return_value = {
                "public_certificate": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
                "fingerprint": "cc" * 32,
            }
            with self.assertRaises((peer_connectivity.PeerPairingRejected, ValueError)):
                peer_connectivity._local_pair_peer(self.settings, attacker_peer, PAIR_CODE)

        for hit in self.hits:
            self.assertNotIn(PAIR_CODE.encode("utf-8"), hit["body"])
            self.assertNotIn(b"pairing_code", hit["body"])
        self.assertFalse(self.hits, "pairing POST was delivered in cleartext to the spoofed listener")
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        self.assertEqual(stored["reachable_url"], "https://127.0.0.1:1")
        self.assertTrue(stored.get("paired_over_tls"))


if __name__ == "__main__":
    unittest.main()
