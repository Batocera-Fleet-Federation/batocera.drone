"""UAT for #599: sibling pollers, omitted TLS flag, and wire-level plaintext 404s.

Derived from the issue before treating the implementation as truth:

- SMTP and Tailnet sharing-revocation pollers use the same GET-and-404
  pattern as VPN. A forged plaintext 404 on every tick must leave the
  imported SMTP password and Tailnet auth key in place.
- An http_only peer record may omit ``paired_over_tls`` (falsey, missing, 0,
  or the string "false"). Absence of a TLS pairing flag is not authentication.
- An IPv6 loopback http route is still cleartext.
- A 404 body that looks like a sharing-off JSON payload is still forgeable
  on http; only a pinned HTTPS 404 is revocation evidence.
- An HTTPError 404 whose filename is an http URL, with no peer_route stamp,
  is unauthenticated.
- A successful plaintext GET (source still sharing) must not revoke either.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import app.device.smtp_manager as smtp_manager
import app.device.tailnet_service as tailnet_service
import app.device.vpn_manager as vpn_manager
from app.common.settings import Settings
from app.transfer import peer_connectivity as pc


SOURCE = "source-http-only-599-wire"
OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"
SMTP_PAYLOAD = {
    "host": "smtp.example.com",
    "port": 587,
    "use_starttls": True,
    "use_ssl": False,
    "username": "me@example.com",
    "password": "smtp-secret-599-wire",
    "from_address": "drone@example.com",
    "recipient_email": "owner@example.com",
}
SHARING_OFF_JSON = b'{"sharing_enabled":false,"has_credentials":false,"auth_key":""}'


def _env_settings(root: Path, device_id: str) -> Settings:
    with mock.patch.dict("os.environ", {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": device_id,
        "DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS": "30",
        "DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS": "30",
        "DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS": "30",
    }, clear=True):
        return Settings.from_env()


def _http_only_peer(port: int, extra: dict | None = None, host: str = "127.0.0.1") -> dict:
    peer = {
        "drone_id": SOURCE,
        "name": "HTTP Only Source",
        "scheme": "http",
        "protocol": "http",
        "local_ip": host,
        "api_port": port,
        "peer_mtls_port": port,
    }
    if extra:
        peer.update(extra)
    return peer


class _StatusServer:
    def __init__(self, status: int, body: bytes = b"not found", host: str = "127.0.0.1") -> None:
        self.hits: list[str] = []
        owner = self
        family = socket.AF_INET6 if ":" in host else socket.AF_INET

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

        class Server(ThreadingHTTPServer):
            address_family = family

        self.server = Server((host, 0), Handler)
        self.host = host
        self.port = int(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def _ipv6_loopback_available() -> bool:
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind(("::1", 0))
        sock.close()
        return True
    except OSError:
        return False


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


class Issue599HttpOnlySiblingPollersAndWireBoundariesUAT(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="issue-599-wire-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=self.root / "install")
        install.start()
        self.addCleanup(install.stop)
        allowed = mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=True)
        allowed.start()
        self.addCleanup(allowed.stop)
        self.settings = _env_settings(self.root, "adversarial-599-wire")
        vpn_manager.import_from_peer(
            self.settings,
            {
                "config_filename": "client.ovpn",
                "config_text": OVPN,
                "has_credentials": True,
                "username": "vpn-user",
                "password": "vpn-pass-599-wire",
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
                self.settings, {"auth_key": "tskey-auth-599-wire", "enrolled": True},
                source_peer_id=SOURCE, source_peer_name="HTTP Only Source",
            )

    def _serve(self, status: int, body: bytes = b"not found", host: str = "127.0.0.1") -> _StatusServer:
        server = _StatusServer(status, body, host=host)
        self.addCleanup(server.close)
        return server

    def _as_paired(self, port: int, extra: dict | None = None, host: str = "127.0.0.1"):
        return mock.patch(
            "app.transfer.local_network.get_paired_peer",
            return_value=_http_only_peer(port, extra, host=host),
        )

    def _assert_secrets_intact(self) -> None:
        vpn_state = vpn_manager._load_state(self.settings)
        self.assertTrue(vpn_state["has_credentials"])
        self.assertEqual(vpn_state["revoked_reason"], "")
        self.assertEqual(
            vpn_manager.auth_path(self.settings).read_text(),
            "vpn-user\nvpn-pass-599-wire\n",
        )
        smtp_state = smtp_manager._load_state(self.settings)
        self.assertEqual(smtp_state["password"], "smtp-secret-599-wire")
        self.assertEqual(smtp_state["revoked_reason"], "")
        tailnet_state = tailnet_service._load_sharing_state(self.settings)
        self.assertEqual(tailnet_state["auth_key"], "tskey-auth-599-wire")
        self.assertEqual(tailnet_state["revoked_reason"], "")

    def _run_poller_until_hits(self, module, poller_name: str, server: _StatusServer, extra: dict | None = None) -> None:
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(_generation: int) -> bool:
            return len(sleeps) > 2

        with self._as_paired(server.port, extra), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect, \
                mock.patch.object(module, "poller_generation", return_value=599), \
                mock.patch.object(module, "poller_retired", new=retired), \
                mock.patch.object(module.time, "sleep", new=fake_sleep):
            getattr(module, poller_name)(self.settings)
        disconnect.assert_not_called()
        self.assertGreaterEqual(len(server.hits), 2)

    def test_smtp_poller_ticks_over_plaintext_404_leave_password(self) -> None:
        server = self._serve(404)
        self._run_poller_until_hits(smtp_manager, "run_sharing_revocation_poller", server)
        self._assert_secrets_intact()
        self.assertTrue(any(path.startswith("/v1/api/peer/smtp/config") for path in server.hits))

    def test_tailnet_poller_ticks_over_plaintext_404_leave_auth_key(self) -> None:
        server = self._serve(404)
        self._run_poller_until_hits(tailnet_service, "run_tailnet_sharing_revocation_poller", server)
        self._assert_secrets_intact()
        self.assertTrue(any(path.startswith("/v1/api/peer/tailnet/config") for path in server.hits))

    def test_omitted_and_falsey_paired_over_tls_http_404_does_not_revoke(self) -> None:
        for extra in (
            {},
            {"paired_over_tls": False},
            {"paired_over_tls": 0},
            {"paired_over_tls": "false"},
            {"paired_over_tls": ""},
        ):
            with self.subTest(extra=extra):
                server = self._serve(404, SHARING_OFF_JSON)
                with self._as_paired(server.port, extra), mock.patch.object(vpn_manager, "disconnect") as disconnect:
                    self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
                    self.assertFalse(smtp_manager.check_sharing_revocation(self.settings))
                    self.assertFalse(tailnet_service.check_tailnet_sharing_revocation(self.settings))
                disconnect.assert_not_called()
                self._assert_secrets_intact()

    def test_plaintext_404_json_sharing_off_body_does_not_revoke(self) -> None:
        server = self._serve(404, SHARING_OFF_JSON)
        with self._as_paired(server.port), mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
        disconnect.assert_not_called()
        self._assert_secrets_intact()
        self.assertTrue(server.hits)

    def test_plaintext_200_still_sharing_does_not_revoke(self) -> None:
        body = b'{"sharing_enabled":true,"has_credentials":true,"auth_key":"tskey-auth-599-wire"}'
        server = self._serve(200, body)
        with self._as_paired(server.port), mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
            self.assertFalse(smtp_manager.check_sharing_revocation(self.settings))
            self.assertFalse(tailnet_service.check_tailnet_sharing_revocation(self.settings))
        disconnect.assert_not_called()
        self._assert_secrets_intact()

    def test_unstamped_http_filename_404_is_unauthenticated(self) -> None:
        error = HTTPError("http://127.0.0.1:9/v1/api/peer/vpn/config", 404, "not found", None, None)
        self.assertNotIn("peer_route", vars(error))
        self.assertFalse(pc.peer_http_error_is_authenticated(error))
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=_http_only_peer(9)), \
                mock.patch.object(pc, "_peer_get_json_for_peer", side_effect=error), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
            self.assertFalse(smtp_manager.check_sharing_revocation(self.settings))
            self.assertFalse(tailnet_service.check_tailnet_sharing_revocation(self.settings))
        disconnect.assert_not_called()
        self._assert_secrets_intact()

    @unittest.skipUnless(_ipv6_loopback_available(), "IPv6 loopback is not available")
    def test_ipv6_loopback_http_404_does_not_revoke(self) -> None:
        server = self._serve(404, SHARING_OFF_JSON, host="::1")
        extra = {"scheme": "http", "protocol": "http", "paired_over_tls": False}
        with self._as_paired(server.port, extra, host="::1"), mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
            self.assertFalse(smtp_manager.check_sharing_revocation(self.settings))
            self.assertFalse(tailnet_service.check_tailnet_sharing_revocation(self.settings))
        disconnect.assert_not_called()
        self._assert_secrets_intact()
        self.assertTrue(server.hits)


if __name__ == "__main__":
    unittest.main()
