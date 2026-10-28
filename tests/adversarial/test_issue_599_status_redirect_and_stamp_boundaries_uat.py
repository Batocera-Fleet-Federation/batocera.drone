"""Boundary UAT for #599: only an authenticated 404 is revocation evidence.

Derived from the issue before treating the implementation as truth:

- Any non-404 status, a 200 body, a timeout, or a malformed payload is
  transient, TLS or not.
- An HTTPError 404 whose dialed candidate was http is forgeable even when
  error.filename / error.url claims https after a redirect.
- _peer_get_json_for_peer must stamp the dialed candidate on HTTPError so the
  revocation check cannot be confused by a redirected URL.
- An http_only peer that also advertises an https URL without a pin must not
  revoke when the https attempt fails closed and the http attempt 404s.
- Mixed-case https on a recorded route is still https.
- Unknown / empty schemes are unauthenticated.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import app.device.smtp_manager as smtp_manager
import app.device.vpn_manager as vpn_manager
from app.common.settings import Settings
from app.transfer import peer_connectivity as pc


SOURCE = "source-boundary"
OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"
SMTP_PAYLOAD = {
    "host": "smtp.example.com",
    "port": 587,
    "use_starttls": True,
    "use_ssl": False,
    "username": "me@example.com",
    "password": "smtp-boundary-599",
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


def _http_only_peer(port: int, extra: dict | None = None) -> dict:
    peer = {
        "drone_id": SOURCE,
        "name": "Boundary Source",
        "scheme": "http",
        "protocol": "http",
        "paired_over_tls": False,
        "local_ip": "127.0.0.1",
        "api_port": port,
        "peer_mtls_port": port,
    }
    if extra:
        peer.update(extra)
    return peer


class _HttpPeer:
    def __init__(self, status: int, body: bytes = b"{}", location: str | None = None) -> None:
        self.hits: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner.hits.append(self.path)
                if location is not None:
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.end_headers()
                    return
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


class Issue599StatusRedirectAndStampBoundariesUAT(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="issue-599-bound-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=self.root / "install")
        install.start()
        self.addCleanup(install.stop)
        allowed = mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=True)
        allowed.start()
        self.addCleanup(allowed.stop)
        self.settings = _env_settings(self.root, "adversarial-599-boundaries")
        vpn_manager.import_from_peer(
            self.settings,
            {
                "config_filename": "client.ovpn",
                "config_text": OVPN,
                "has_credentials": True,
                "username": "vpn-user",
                "password": "vpn-boundary-599",
            },
            source_peer_id=SOURCE,
            source_peer_name="Boundary Source",
        )
        smtp_manager.import_from_peer(
            self.settings, SMTP_PAYLOAD, source_peer_id=SOURCE, source_peer_name="Boundary Source",
        )

    def _serve(self, status: int, body: bytes = b"{}", location: str | None = None) -> _HttpPeer:
        server = _HttpPeer(status, body, location)
        self.addCleanup(server.close)
        return server

    def _paired(self, port: int, extra: dict | None = None):
        return mock.patch(
            "app.transfer.local_network.get_paired_peer",
            return_value=_http_only_peer(port, extra),
        )

    def _assert_vpn_intact(self) -> None:
        state = vpn_manager._load_state(self.settings)
        self.assertTrue(state["has_credentials"])
        self.assertEqual(state["revoked_reason"], "")
        self.assertEqual(vpn_manager.auth_path(self.settings).read_text(), "vpn-user\nvpn-boundary-599\n")

    def test_live_non_404_http_statuses_are_transient(self) -> None:
        for status, body in (
            (200, b'{"has_credentials":false,"sharing_enabled":false}'),
            (204, b""),
            (401, b"unauthorized"),
            (403, b"forbidden"),
            (410, b"gone"),
            (500, b"error"),
            (503, b"busy"),
        ):
            with self.subTest(status=status):
                server = self._serve(status, body)
                with self._paired(server.port), mock.patch.object(vpn_manager, "disconnect") as disconnect:
                    self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
                disconnect.assert_not_called()
                self._assert_vpn_intact()

    def test_malformed_json_200_is_transient(self) -> None:
        server = self._serve(200, b"not-json{")
        with self._paired(server.port), mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
        disconnect.assert_not_called()
        self._assert_vpn_intact()

    def test_http_redirect_to_http_404_does_not_revoke(self) -> None:
        terminal = self._serve(404, b"gone")
        start = self._serve(302, location=f"http://127.0.0.1:{terminal.port}/v1/api/peer/vpn/config")
        with self._paired(start.port), mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
        disconnect.assert_not_called()
        self._assert_vpn_intact()
        self.assertTrue(terminal.hits)

    def test_dialed_http_route_wins_when_error_url_claims_https(self) -> None:
        # On-path attacker 404s, urllib reports a redirected https filename.
        # _peer_get_json_for_peer must stamp the http candidate it actually dialed.
        raised = HTTPError("https://forged.example/v1/api/peer/vpn/config", 404, "not found", None, None)
        with mock.patch.object(pc, "_peer_address_candidates", return_value=["http://127.0.0.1:9"]), \
                mock.patch.object(pc, "_candidate_dialable", return_value=True), \
                mock.patch.object(pc, "_peer_get_json", side_effect=raised), \
                mock.patch("app.transfer.local_network.get_paired_peer", return_value=_http_only_peer(9)), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
            self.assertFalse(smtp_manager.check_sharing_revocation(self.settings))
        disconnect.assert_not_called()
        self.assertEqual(raised.peer_route, "http://127.0.0.1:9")
        self.assertFalse(pc.peer_http_error_is_authenticated(raised))
        self._assert_vpn_intact()
        self.assertEqual(smtp_manager._load_state(self.settings)["password"], "smtp-boundary-599")

    def test_peer_get_json_for_peer_stamps_dialed_http_route_on_real_http_error(self) -> None:
        server = self._serve(404, b"gone")
        peer = _http_only_peer(server.port)
        with self.assertRaises(HTTPError) as caught:
            pc._peer_get_json_for_peer(peer, "/v1/api/peer/vpn/config", self.settings, peer_id=SOURCE)
        error = caught.exception
        self.assertEqual(error.code, 404)
        self.assertEqual(error.peer_route, f"http://127.0.0.1:{server.port}")
        self.assertFalse(pc.peer_http_error_is_authenticated(error))

    def test_https_reachable_url_without_pin_then_http_404_does_not_revoke(self) -> None:
        server = self._serve(404, b"gone")
        extra = {"reachable_url": "https://127.0.0.1:1"}
        with self._paired(server.port, extra), mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
        disconnect.assert_not_called()
        self._assert_vpn_intact()
        self.assertTrue(server.hits)

    def test_mixed_case_https_recorded_route_alone_is_not_authenticated(self) -> None:
        # peer_route is diagnostic only (#599); verified-TLS evidence is what authenticates.
        error = HTTPError("HTTP://peer/x", 404, "not found", None, None)
        error.peer_route = "HTTPS://127.0.0.1:443"
        self.assertFalse(pc.peer_http_error_is_authenticated(error))
        error._peer_tls_evidence = pc._PEER_TLS_EVIDENCE
        self.assertTrue(pc.peer_http_error_is_authenticated(error))
        http_error = HTTPError("https://elsewhere/x", 404, "not found", None, None)
        http_error.peer_route = "HTTP://127.0.0.1:80"
        self.assertFalse(pc.peer_http_error_is_authenticated(http_error))

    def test_unknown_and_non_tls_schemes_are_unauthenticated(self) -> None:
        for route in ("ftp://peer/x", "file:///etc/passwd", "peer.example", "//peer/x", "http://127.0.0.1"):
            with self.subTest(route=route):
                error = HTTPError("https://cover/x", 404, "not found", None, None)
                error.peer_route = route
                self.assertFalse(pc.peer_http_error_is_authenticated(error))
        http_filename = HTTPError("http://peer/x", 404, "not found", None, None)
        self.assertFalse(pc.peer_http_error_is_authenticated(http_filename))

    def test_timeout_and_urlerror_are_transient(self) -> None:
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=_http_only_peer(9)), \
                mock.patch.object(pc, "_peer_get_json_for_peer", side_effect=URLError("timed out")), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
        disconnect.assert_not_called()
        self._assert_vpn_intact()

    def test_http_404_then_later_pinned_https_filename_without_stamp_is_not_used_by_live_client(self) -> None:
        # After a plaintext 404, secrets remain so a later genuine TLS 404 can still act.
        server = self._serve(404, b"gone")
        with self._paired(server.port):
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
        self._assert_vpn_intact()
        tls_404 = HTTPError("https://peer/v1/api/peer/vpn/config", 404, "not found", None, None)
        tls_404.peer_route = "https://127.0.0.1:443"
        # A route stamp alone is diagnostic; only process-local TLS evidence authorises revocation.
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=_http_only_peer(443, {"scheme": "https", "paired_over_tls": True})), \
                mock.patch.object(pc, "_peer_get_json_for_peer", side_effect=tls_404):
            self.assertFalse(vpn_manager.check_sharing_revocation(self.settings))
        self._assert_vpn_intact()
        tls_404._peer_tls_evidence = pc._PEER_TLS_EVIDENCE
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=_http_only_peer(443, {"scheme": "https", "paired_over_tls": True})), \
                mock.patch.object(pc, "_peer_get_json_for_peer", side_effect=tls_404):
            self.assertTrue(vpn_manager.check_sharing_revocation(self.settings))
        self.assertFalse(vpn_manager._load_state(self.settings)["has_credentials"])


if __name__ == "__main__":
    unittest.main()
