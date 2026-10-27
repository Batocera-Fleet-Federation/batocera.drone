"""UAT for #599: a 404 over pinned HTTPS is still genuine revocation.

The fix refuses to act on a 404 obtained over a non-TLS route. A 404 that
arrives on the pinned, authenticated channel is the source turning sharing off
and must still disconnect the VPN, wipe auth.txt, clear the SMTP password, and
clear the imported Tailnet auth key. Missing pins stay transient (SSLError),
so an https URL without a cached cert cannot forge revocation either.
"""

from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import app.device.smtp_manager as smtp_manager
import app.device.tailnet_service as tailnet_service
import app.device.vpn_manager as vpn_manager
from app.common.settings import Settings
from app.transfer import peer_connectivity as pc


SOURCE = "source-tls"
OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"
SMTP_PAYLOAD = {
    "host": "smtp.example.com",
    "port": 587,
    "use_starttls": True,
    "use_ssl": False,
    "username": "me@example.com",
    "password": "smtp-secret-599-tls",
    "from_address": "drone@example.com",
    "recipient_email": "owner@example.com",
}


def _env_settings(root: Path, device_id: str) -> Settings:
    with mock.patch.dict("os.environ", {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": device_id,
    }, clear=True):
        return Settings.from_env()


def _tls_peer(port: int, peer_id: str = SOURCE) -> dict:
    return {
        "drone_id": peer_id,
        "name": "TLS Source",
        "scheme": "https",
        "protocol": "https",
        "paired_over_tls": True,
        "local_ip": "127.0.0.1",
        "api_port": port,
        "peer_mtls_port": port,
    }


class _Tls404Peer:
    def __init__(self, cert: Path, key: Path) -> None:
        self.hits: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner.hits.append(self.path)
                body = b'{"error":"not sharing"}'
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = int(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def _status_json_result() -> mock.Mock:
    payload = {
        "BackendState": "Running",
        "Version": "1.70.0",
        "Self": {"TailscaleIPs": ["100.64.0.5"], "DNSName": "drone.tailxyz.ts.net.", "ID": "self-id"},
        "CurrentTailnet": {"Name": "example.ts.net", "MagicDNSSuffix": "tailxyz.ts.net"},
        "Peer": {},
    }
    return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")


def _fake_tailscale_cli() -> mock.MagicMock:
    cli = mock.MagicMock()
    cli.exists.return_value = True
    cli.__str__.return_value = "/userdata/system/tailscale/bin/tailscale"
    return cli


def _fake_subprocess_run(cli_args, **_kwargs):
    if "status" in cli_args and "--json" in cli_args:
        return _status_json_result()
    return mock.Mock(returncode=0, stdout="", stderr="")


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class Issue599PinnedHttps404StillRevokesUAT(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.keys = tempfile.TemporaryDirectory(prefix="issue-599-tls-identities-")
        root = Path(cls.keys.name)
        cls.cert = root / "peer.crt"
        cls.key = root / "peer.key"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(cls.key), "-out", str(cls.cert), "-days", "1",
                "-subj", "/CN=127.0.0.1",
                "-addext", "subjectAltName=IP:127.0.0.1",
            ],
            check=True, capture_output=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.keys.cleanup()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="issue-599-tls-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=self.root / "install")
        install.start()
        self.addCleanup(install.stop)
        allowed = mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=True)
        allowed.start()
        self.addCleanup(allowed.stop)
        self.settings = _env_settings(self.root, "adversarial-599-https")
        pc._save_local_peer_certificate(self.settings, SOURCE, self.cert.read_text(encoding="utf-8"))
        vpn_manager.import_from_peer(
            self.settings,
            {
                "config_filename": "client.ovpn",
                "config_text": OVPN,
                "has_credentials": True,
                "username": "vpn-user",
                "password": "vpn-pass-599-tls",
            },
            source_peer_id=SOURCE,
            source_peer_name="TLS Source",
        )
        smtp_manager.import_from_peer(
            self.settings, SMTP_PAYLOAD, source_peer_id=SOURCE, source_peer_name="TLS Source",
        )
        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
            tailnet_service.import_tailnet_from_peer(
                self.settings, {"auth_key": "tskey-auth-599-tls", "enrolled": True},
                source_peer_id=SOURCE, source_peer_name="TLS Source",
            )
        self.peer_server = _Tls404Peer(self.cert, self.key)
        self.addCleanup(self.peer_server.close)

    def _paired(self):
        return mock.patch(
            "app.transfer.local_network.get_paired_peer",
            return_value=_tls_peer(self.peer_server.port),
        )

    def test_pinned_https_404_revokes_vpn_smtp_and_tailnet(self) -> None:
        with self._paired():
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
            self.assertTrue(smtp_manager.check_sharing_revocation(self.settings))
            self.assertTrue(tailnet_service.check_tailnet_sharing_revocation(self.settings))
        vpn_state = vpn_manager._load_state(self.settings)
        self.assertFalse(vpn_state["has_credentials"])
        self.assertIn("turned off sharing", vpn_state["revoked_reason"])
        self.assertEqual(vpn_state["source_peer_id"], SOURCE)
        self.assertFalse(vpn_manager.auth_path(self.settings).exists())
        self.assertTrue(vpn_manager.config_path(self.settings).is_file())
        smtp_state = smtp_manager._load_state(self.settings)
        self.assertEqual(smtp_state["password"], "")
        self.assertIn("turned off sharing", smtp_state["revoked_reason"])
        self.assertEqual(smtp_state["host"], "smtp.example.com")
        tailnet_state = tailnet_service._load_sharing_state(self.settings)
        self.assertEqual(tailnet_state["auth_key"], "")
        self.assertIn("turned off sharing", tailnet_state["revoked_reason"])
        self.assertIn("/v1/api/peer/vpn/config", self.peer_server.hits)
        self.assertIn("/v1/api/peer/smtp/config", self.peer_server.hits)
        self.assertIn("/v1/api/peer/tailnet/config", self.peer_server.hits)

    def test_https_without_cached_pin_is_transient_and_does_not_revoke(self) -> None:
        pc._local_peer_cert_cache_path(self.settings, SOURCE).unlink()
        with self._paired(), mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
        disconnect.assert_not_called()
        state = vpn_manager._load_state(self.settings)
        self.assertTrue(state["has_credentials"])
        self.assertEqual(state["revoked_reason"], "")
        self.assertEqual(vpn_manager.auth_path(self.settings).read_text(), "vpn-user\nvpn-pass-599-tls\n")


if __name__ == "__main__":
    unittest.main()
