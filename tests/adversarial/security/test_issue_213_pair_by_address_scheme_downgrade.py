"""A peer-info response must not downgrade pair-by-address to a cleartext, unpinned send (issue #213).

The operator types an https address (and, for an unpaired Drone, the fingerprint
from the real Drone's pairing screen). The peer-info JSON comes from whoever
answers at that address, so its ``scheme``/port fields must not be able to turn
the pairing-code POST into ``http://`` -- which skips the certificate pin.
"""

import unittest
from unittest import mock

from app.transfer import peer_connectivity
from app.web import handlers_network

REAL_FINGERPRINT = "ab" * 32


def _hostile_info() -> dict:
    return {
        "service": handlers_network._local_network.DISCOVERY_SERVICE,
        "drone_id": "den-cabinet",
        "name": "Den",
        "scheme": "http",
        "api_port": 443,
        "peer_mtls_port": 8080,
        "reachable_url": "http://10.66.0.9:8080",
        "certificate_fingerprint": "bb" * 32,
    }


class PairByAddressSchemeDowngradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False, device_id="local-a")
        self.handler.server = mock.MagicMock()
        self.handler._send_json = mock.Mock()
        self.sent: list[dict] = []

    def _run(self, fingerprint: str) -> None:
        def record(settings, peer, code, **kwargs):
            self.sent.append({"address": peer_connectivity._peer_address(peer), "code": code, "fp": kwargs.get("confirmed_fingerprint")})
            return {"drone_id": "den-cabinet", "certificate_path": "/nonexistent/none.pem"}

        with mock.patch.object(handlers_network._local_network, "is_local_mode", return_value=True), \
                mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value=None), \
                mock.patch.object(handlers_network, "_fetch_peer_info", return_value=_hostile_info()), \
                mock.patch.object(handlers_network, "_local_pair_peer", side_effect=record):
            self.handler._handle_admin_local_peer_pair_by_address(
                {"address": "https://10.66.0.9", "pairing_code": "12345678", "certificate_fingerprint": fingerprint}
            )

    def test_pairing_code_is_not_sent_in_cleartext_without_fingerprint_confirmation(self) -> None:
        self._run("")
        self.assertEqual(
            [row for row in self.sent if row["address"].startswith("http://")],
            [],
            "operator typed https:// but the pairing code was handed to a cleartext address chosen by peer-info",
        )

    def test_fingerprint_confirmed_pairing_stays_on_the_https_address_the_operator_typed(self) -> None:
        self._run(REAL_FINGERPRINT)
        for row in self.sent:
            self.assertTrue(row["address"].startswith("https://"), row["address"])


if __name__ == "__main__":
    unittest.main()
