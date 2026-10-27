"""UAT for #599: a forged plaintext 404 must not wipe imported secrets.

Issue #599: check_sharing_revocation GETs /v1/api/peer/{vpn,smtp,tailnet}/config
over whatever route the paired record holds. For http_only peers that route is
cleartext and unauthenticated. An on-path attacker who answers 404 must not be
able to disconnect a tunnel, delete auth.txt / SMTP password / Tailnet auth key,
or record a 'turned off sharing' revocation. The same forged 404 repeated every
poll interval must keep leaving the secrets in place. Transient-failure
behavior (return False, change nothing) is required.

'No longer paired' is local roster evidence and is unaffected: that path still
revokes. A 404 is revocation only when it arrived over a pinned, authenticated
TLS channel.
"""

from __future__ import annotations

import json
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


SOURCE = "source-http-only"
OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"
SMTP_PAYLOAD = {
    "host": "smtp.example.com",
    "port": 587,
    "use_starttls": True,
    "use_ssl": False,
    "username": "me@example.com",
    "password": "smtp-secret-599",
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
        "DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS": "30",
    }, clear=True):
        return Settings.from_env()


def _http_only_peer(port: int, peer_id: str = SOURCE) -> dict:
    return {
        "drone_id": peer_id,
        "name": "HTTP Only Source",
        "scheme": "http",
        "protocol": "http",
        "paired_over_tls": False,
        "local_ip": "127.0.0.1",
        "api_port": port,
        "peer_mtls_port": port,
    }


class _StatusServer:
    def __init__(self, status: int, body: bytes = b"not found") -> None:
        self.hits: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner.hits.append(self.path)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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


class Issue599Plaintext404LiveUAT(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="issue-599-http-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=self.root / "install")
        install.start()
        self.addCleanup(install.stop)
        allowed = mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=True)
        allowed.start()
        self.addCleanup(allowed.stop)
        self.settings = _env_settings(self.root, "adversarial-599-plaintext")
        vpn_manager.import_from_peer(
            self.settings,
            {
                "config_filename": "client.ovpn",
                "config_text": OVPN,
                "has_credentials": True,
                "username": "vpn-user",
                "password": "vpn-pass-599",
            },
            source_peer_id=SOURCE,
            source_peer_name="HTTP Only Source",
        )
        smtp_manager.import_from_peer(
            self.settings, SMTP_PAYLOAD, source_peer_id=SOURCE, source_peer_name="HTTP Only Source",
        )
        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
            tailnet_service.import_tailnet_from_peer(
                self.settings, {"auth_key": "tskey-auth-599-shared", "enrolled": True},
                source_peer_id=SOURCE, source_peer_name="HTTP Only Source",
            )

    def _serve(self, status: int, body: bytes = b"not found") -> _StatusServer:
        server = _StatusServer(status, body)
        self.addCleanup(server.close)
        return server

    def _as_paired(self, port: int):
        return mock.patch("app.transfer.local_network.get_paired_peer", return_value=_http_only_peer(port))

    def _assert_vpn_intact(self) -> None:
        state = vpn_manager._load_state(self.settings)
        self.assertTrue(state["has_credentials"])
        self.assertEqual(state["username"], "vpn-user")
        self.assertEqual(state["revoked_reason"], "")
        self.assertIsNone(state["revoked_at"])
        self.assertEqual(state["source_peer_id"], SOURCE)
        self.assertEqual(vpn_manager.auth_path(self.settings).read_text(), "vpn-user\nvpn-pass-599\n")
        self.assertTrue(vpn_manager.config_path(self.settings).is_file())

    def test_live_http_404_does_not_wipe_vpn_smtp_or_tailnet_secrets(self) -> None:
        server = self._serve(404)
        peer_patch = self._as_paired(server.port)
        with peer_patch, mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
            self.assertFalse(smtp_manager.check_sharing_revocation(self.settings))
            self.assertFalse(tailnet_service.check_tailnet_sharing_revocation(self.settings))
        disconnect.assert_not_called()
        self._assert_vpn_intact()
        smtp_state = smtp_manager._load_state(self.settings)
        self.assertEqual(smtp_state["password"], "smtp-secret-599")
        self.assertEqual(smtp_state["revoked_reason"], "")
        self.assertEqual(smtp_state["host"], "smtp.example.com")
        tailnet_state = tailnet_service._load_sharing_state(self.settings)
        self.assertEqual(tailnet_state["auth_key"], "tskey-auth-599-shared")
        self.assertEqual(tailnet_state["revoked_reason"], "")
        self.assertIn("/v1/api/peer/vpn/config", server.hits)
        self.assertIn("/v1/api/peer/smtp/config", server.hits)
        self.assertIn("/v1/api/peer/tailnet/config", server.hits)

    def test_repeated_forged_http_404_keeps_vpn_down_attack_from_succeeding(self) -> None:
        # Availability attack: answer 404 on every poll. Secrets must survive.
        server = self._serve(404)
        with self._as_paired(server.port), mock.patch.object(vpn_manager, "disconnect") as disconnect:
            for _ in range(5):
                self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
        disconnect.assert_not_called()
        self._assert_vpn_intact()
        self.assertGreaterEqual(len(server.hits), 5)

    def test_poller_ticks_over_plaintext_404_leave_credentials_in_place(self) -> None:
        server = self._serve(404)
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(_generation: int) -> bool:
            return len(sleeps) > 2

        with self._as_paired(server.port), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect, \
                mock.patch.object(vpn_manager, "poller_generation", return_value=7), \
                mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep):
            vpn_manager.run_sharing_revocation_poller(self.settings)
        disconnect.assert_not_called()
        self._assert_vpn_intact()
        self.assertGreaterEqual(len(server.hits), 2)

    def test_peer_gone_still_revokes_because_that_evidence_is_local(self) -> None:
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None):
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
        state = vpn_manager._load_state(self.settings)
        self.assertFalse(state["has_credentials"])
        self.assertIn("no longer paired", state["revoked_reason"])
        self.assertEqual(state["source_peer_id"], SOURCE)
        self.assertFalse(vpn_manager.auth_path(self.settings).exists())


if __name__ == "__main__":
    unittest.main()
