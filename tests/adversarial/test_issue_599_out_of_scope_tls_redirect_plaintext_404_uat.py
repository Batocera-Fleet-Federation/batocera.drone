"""Out of scope for #599: a TLS dial that redirects to a plaintext 404 still revokes.

#599's delivery is the http_only on-path 404. The check keys authentication off
the *dialed candidate* (`HTTPError.peer_route`), not the hop that produced the
status. urllib follows a 302 from a pinned https peer onto a cleartext URL; the
resulting 404 is then treated as authenticated sharing-off because the original
candidate was https.

That 404 did not arrive over the pinned TLS channel. The issue's remediation
says revocation evidence must come from that channel. Exploiting this needs the
TLS peer (or a broken pin) to emit the redirect, so it is a separate labelled
issue rather than a delivery gate for the http_only fix. Same shape on VPN,
SMTP, and Tailnet.
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


SOURCE = "source-redir"
OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"
SMTP_PAYLOAD = {
    "host": "smtp.example.com",
    "port": 587,
    "use_starttls": True,
    "use_ssl": False,
    "username": "me@example.com",
    "password": "smtp-redir-599",
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


class _RedirectTls:
    def __init__(self, cert: Path, key: Path, location: str) -> None:
        self.hits: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner.hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", location)
                self.end_headers()

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


class _Plain404:
    def __init__(self) -> None:
        self.hits: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner.hits.append(self.path)
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"not found"}')

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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
class Issue599OutOfScopeTlsRedirectPlaintext404UAT(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.keys = tempfile.TemporaryDirectory(prefix="issue-599-redir-identities-")
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
        tmp = tempfile.TemporaryDirectory(prefix="issue-599-redir-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=self.root / "install")
        install.start()
        self.addCleanup(install.stop)
        allowed = mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=True)
        allowed.start()
        self.addCleanup(allowed.stop)
        self.settings = _env_settings(self.root, "adversarial-599-redir")
        pc._save_local_peer_certificate(self.settings, SOURCE, self.cert.read_text(encoding="utf-8"))
        vpn_manager.import_from_peer(
            self.settings,
            {
                "config_filename": "client.ovpn",
                "config_text": OVPN,
                "has_credentials": True,
                "username": "vpn-user",
                "password": "vpn-redir-599",
            },
            source_peer_id=SOURCE,
            source_peer_name="Redirect Source",
        )
        smtp_manager.import_from_peer(
            self.settings, SMTP_PAYLOAD, source_peer_id=SOURCE, source_peer_name="Redirect Source",
        )
        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
            tailnet_service.import_tailnet_from_peer(
                self.settings, {"auth_key": "tskey-auth-599-redir", "enrolled": True},
                source_peer_id=SOURCE, source_peer_name="Redirect Source",
            )
        self.plain = _Plain404()
        self.addCleanup(self.plain.close)
        self.tls = _RedirectTls(self.cert, self.key, f"http://127.0.0.1:{self.plain.port}/v1/api/peer/vpn/config")
        self.addCleanup(self.tls.close)

    def _peer(self) -> dict:
        return {
            "drone_id": SOURCE,
            "name": "Redirect Source",
            "scheme": "https",
            "paired_over_tls": True,
            "local_ip": "127.0.0.1",
            "api_port": self.tls.port,
            "peer_mtls_port": self.tls.port,
        }

    def test_https_redirect_to_plaintext_404_must_not_count_as_authenticated_revocation(self) -> None:
        peer = self._peer()
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            vpn_revoked = vpn_manager.check_sharing_revocation(self.settings)
            smtp_revoked = smtp_manager.check_sharing_revocation(self.settings)
            tailnet_revoked = tailnet_service.check_tailnet_sharing_revocation(self.settings)
        self.assertFalse(
            vpn_revoked,
            "a 404 that urllib followed onto http:// after a pinned https 302 is not TLS evidence",
        )
        self.assertFalse(smtp_revoked)
        self.assertFalse(tailnet_revoked)
        disconnect.assert_not_called()
        self.assertEqual(vpn_manager.auth_path(self.settings).read_text(), "vpn-user\nvpn-redir-599\n")
        self.assertEqual(smtp_manager._load_state(self.settings)["password"], "smtp-redir-599")
        self.assertEqual(tailnet_service._load_sharing_state(self.settings)["auth_key"], "tskey-auth-599-redir")
        self.assertTrue(self.plain.hits)


if __name__ == "__main__":
    unittest.main()
