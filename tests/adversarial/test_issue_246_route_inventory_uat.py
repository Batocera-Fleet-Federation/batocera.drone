"""Issue #246 UAT: swarm inventory, BIOS/file pull, and LAN transport after a spoof.

The issue's attacker is an on-LAN host that announces scheme=http for an
already-paired drone_id. Health workers, the swarm overview probe, inventory
browse, BIOS download, config-backup file pull, and LanDirectTransport.fetch
must not dial that plaintext listener. Pairing must consult the persisted TLS
pin even when the discovered dict omits paired_over_tls.

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
from app.transport.base import DownloadRequest, TransferContext
from app.transport.lan import LanDirectTransport
from app.web import handlers_network

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
        self.hits.append({"path": self.path, "method": self.command, "body": body})

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        payload = json.dumps({"status": "ok", "files": [], "counts": {}}).encode("utf-8")
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


class Issue246RouteInventoryUAT(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hits: list = []
        _PlaintextPeer.hits = cls.hits
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _PlaintextPeer)
        cls.port = int(cls.server.server_address[1])
        cls.thread = threading.Thread(target=cls.server.serve_forever, name="issue-246-inv", daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.hits.clear()
        temp = tempfile.TemporaryDirectory(prefix="issue-246-inv-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = _settings(self.root)
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)
        self._identity = mock.patch.object(
            peer_connectivity, "_identity_swarm_networking_allowed", return_value=True
        )
        self._identity.start()
        self.addCleanup(self._identity.stop)

    def _pin_tls(self, **updates) -> dict:
        peer = {
            "drone_id": PEER_ID,
            "name": "Victim",
            "hostname": "victim.local",
            "reachable_url": "https://127.0.0.1:1",
            "advertised_reachable_url": "https://victim.local:7443",
            "scheme": "https",
            "api_port": 7443,
            "peer_mtls_port": 8443,
            "certificate_fingerprint": FINGERPRINT,
            "paired_over_tls": True,
            "public_ip": "203.0.113.8",
            "local_ip": "192.0.2.20",
        }
        peer.update(updates)
        return local_network.save_paired_peer(self.settings, peer)

    def _spoof(self, **updates) -> dict:
        payload = {
            "service": local_network.DISCOVERY_SERVICE,
            "drone_id": PEER_ID,
            "scheme": "http",
            "api_port": self.port,
            "peer_mtls_port": self.port,
            "reachable_url": f"http://127.0.0.1:{self.port}",
            "hostname": "attacker.local",
            "name": "Attacker cabinet",
            "certificate_fingerprint": "bb" * 32,
            "paired_over_tls": False,
            "local_ip": "127.0.0.1",
            "public_ip": "203.0.113.8",
            "tailnet_ip": "100.64.0.66",
        }
        payload.update(updates)
        return local_network.record_discovered_peer(self.settings, payload, "127.0.0.1")

    def test_swarm_probe_and_inventory_do_not_hit_plaintext_listener(self) -> None:
        self._pin_tls()
        self._spoof()
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        handler = handlers_network.HandlersNetworkMixin()
        handler.settings = self.settings
        handler._send_json = mock.Mock()

        entry = handler._swarm_probe_peer(stored)
        self.assertFalse(entry.get("online"), entry)
        self.assertFalse(self.hits, self.hits)

        with self.assertRaises(Exception):
            handler._peer_inventory_request(stored, PEER_ID, "roms", {})
        self.assertFalse(self.hits, self.hits)

        try:
            handler._handle_admin_local_peer_assets(PEER_ID, {"type": ["roms"]})
        except Exception:
            pass
        else:
            status = handler._send_json.call_args.args[0]
            self.assertNotEqual(status, 200)
        self.assertFalse(self.hits, self.hits)

    def test_bios_and_peer_file_helpers_do_not_fetch_over_plaintext(self) -> None:
        self._pin_tls()
        self._spoof()
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        self.settings.bios_root.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(Exception):
            peer_download._download_bios_from_peer(self.settings, {}, stored, "attacker.bin")
        leftover = self.settings.bios_root / "attacker.bin"
        self.assertFalse(leftover.exists())
        dest = self.root / "backup.bin"
        with self.assertRaises(Exception):
            peer_connectivity._peer_download_file_for_peer(
                stored,
                "/v1/api/peer/config-backups/evil",
                dest,
                self.settings,
                peer_id=PEER_ID,
                timeout=0.5,
            )
        self.assertFalse(dest.exists())
        self.assertFalse(self.hits, self.hits)

    def test_lan_transport_fetch_does_not_hit_plaintext_after_spoof(self) -> None:
        self._pin_tls()
        self._spoof()
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        poisoned = {
            **stored,
            "scheme": "http",
            "api_port": self.port,
            "peer_mtls_port": self.port,
            "local_ip": "127.0.0.1",
            "public_ip": "203.0.113.8",
            "reachable_url": f"http://127.0.0.1:{self.port}",
        }

        def fetch_fn(request, context):
            return peer_download._download_rom_from_peer(
                context.settings,
                context.config,
                context.peer,
                request.system,
                request.relative_path,
            )

        transport = LanDirectTransport(
            fetch_fn=fetch_fn,
            local_network=lambda: {"public_ip": "203.0.113.8", "ipv4": ["192.0.2.1"]},
        )
        lan_url = transport.lan_url(poisoned)
        self.assertIsNotNone(lan_url)
        # A TLS-paired peer must remain on HTTPS even after this record is
        # poisoned with cleartext route metadata.
        self.assertTrue(str(lan_url).lower().startswith("https://"), lan_url)
        ctx = TransferContext(settings=self.settings, repository=None, config={}, peer=poisoned)
        (self.settings.roms_root / "nes").mkdir(parents=True, exist_ok=True)
        with self.assertRaises(Exception):
            transport.fetch(DownloadRequest(asset_type="rom", system="nes", relative_path="attack.nes"), ctx)
        self.assertFalse(self.hits, self.hits)
        self.assertFalse((self.settings.roms_root / "nes" / "attack.nes").exists())

    def test_pair_consults_persisted_tls_pin_when_discovered_row_omits_flag(self) -> None:
        self._pin_tls()
        self._spoof()
        discovered = {
            "drone_id": PEER_ID,
            "scheme": "http",
            "reachable_url": f"http://127.0.0.1:{self.port}",
        }
        self.assertFalse(discovered.get("paired_over_tls"))
        with mock.patch.object(peer_connectivity, "DroneCertificateManager") as manager:
            manager.return_value.ensure_certificate.return_value = {
                "public_certificate": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
                "fingerprint": "cc" * 32,
            }
            with self.assertRaises((peer_connectivity.PeerPairingRejected, ValueError)):
                peer_connectivity._local_pair_peer(self.settings, discovered, PAIR_CODE)
        self.assertFalse(self.hits, self.hits)
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        self.assertEqual(stored["reachable_url"], "https://127.0.0.1:1")
        self.assertTrue(stored.get("paired_over_tls"))


if __name__ == "__main__":
    unittest.main()
