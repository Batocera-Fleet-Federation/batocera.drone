"""Issue #331 UAT: authorization must not permit a TLS-to-HTTP trust downgrade.

The inbound pairing endpoint accepts either a one-time pairing code or an
authorized tailnet source.  Both forms establish *who may pair*, not permission
to weaken an existing TLS-paired record.  A rejected request must leave the
durable peer record alone and must not replace its pinned certificate.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network
from app.web import handlers_peer


FINGERPRINT = "aa" * 32
PEER_ID = "tls-peer-331"
SOURCE_IP = "100.64.0.66"


def _settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "DRONE_DEVICE_ID": "local-331",
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "HTTP_ONLY": "0",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class Issue331AuthorizedPairTlsDowngradeUAT(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="issue-331-uat-")
        self.addCleanup(temporary.cleanup)
        self.settings = _settings(Path(temporary.name))
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)
        self.before = local_network.save_paired_peer(
            self.settings,
            {
                "drone_id": PEER_ID,
                "name": "TLS peer",
                "scheme": "https",
                "reachable_url": "https://192.0.2.31:7443",
                "advertised_reachable_url": "https://peer-331.example:7443",
                "api_port": 7443,
                "peer_mtls_port": 8443,
                "certificate_fingerprint": FINGERPRINT,
                "certificate_path": "/pins/original-331.crt",
                "paired_over_tls": True,
            },
        )

    def _request(
        self, payload: dict, *, tailnet_ips: set[str] | None = None
    ) -> tuple[mock.MagicMock, mock.MagicMock, mock.MagicMock, mock.MagicMock]:
        handler = handlers_peer.HandlersPeerMixin()
        handler.settings = self.settings
        handler.client_address = (SOURCE_IP, 4141)
        handler.server = mock.MagicMock()
        handler._send_json = mock.MagicMock()
        with mock.patch.object(handlers_peer._local_network, "validate_pairing_code", return_value=True) as valid_code, \
                mock.patch.object(handlers_peer, "tailnet_peer_ips", return_value=tailnet_ips or set()), \
                mock.patch.object(handlers_peer, "_save_local_peer_certificate") as save_certificate, \
                mock.patch.object(handlers_peer._local_network, "pairing_code") as rotate_code:
            handler._handle_peer_pair(payload)
        return handler._send_json, save_certificate, valid_code, rotate_code

    def _assert_untouched(self, response: mock.MagicMock, save_certificate: mock.MagicMock, rotate_code: mock.MagicMock) -> None:
        response.assert_called_once()
        self.assertEqual(response.call_args.args[0], 409)
        self.assertIn("plaintext", response.call_args.args[1]["error"].lower())
        save_certificate.assert_not_called()
        rotate_code.assert_not_called()
        self.assertEqual(local_network.get_paired_peer(self.settings, PEER_ID), self.before)

    def test_valid_pairing_code_cannot_repair_tls_peer_as_plain_http(self) -> None:
        response, save_certificate, valid_code, rotate_code = self._request(
            {
                "pairing_code": "accepted-code",
                "drone_id": PEER_ID,
                "scheme": " HTTP ",
                "certificate_pem": "not examined after downgrade rejection",
                "reachable_url": "http://attacker.invalid:8080",
                "api_port": 8080,
            }
        )
        valid_code.assert_called_once_with(self.settings, "accepted-code")
        self._assert_untouched(response, save_certificate, rotate_code)

    def test_tailnet_auto_pair_cannot_repair_tls_peer_as_plain_http(self) -> None:
        response, save_certificate, valid_code, rotate_code = self._request(
            {
                "tailnet_auto_pair": True,
                "drone_id": PEER_ID,
                "scheme": "http",
                "certificate_pem": "not examined after downgrade rejection",
                "reachable_url": "http://attacker.invalid:8080",
                "api_port": 8080,
            },
            tailnet_ips={SOURCE_IP},
        )
        valid_code.assert_not_called()
        self._assert_untouched(response, save_certificate, rotate_code)

    def test_durable_save_cannot_clear_tls_marker_with_falsy_payload(self) -> None:
        stored = local_network.save_paired_peer(
            self.settings,
            {
                "drone_id": PEER_ID,
                "scheme": "http",
                "reachable_url": "http://attacker.invalid:8080",
                "paired_over_tls": 0,
            },
        )
        self.assertTrue(stored["paired_over_tls"])
        self.assertEqual(stored["scheme"], "https")
        self.assertEqual(stored["reachable_url"], self.before["reachable_url"])
        self.assertEqual(stored["certificate_path"], self.before["certificate_path"])


if __name__ == "__main__":
    unittest.main()
