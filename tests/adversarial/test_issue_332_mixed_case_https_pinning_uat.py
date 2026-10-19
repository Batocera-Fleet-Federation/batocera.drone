"""Issue #332 UAT: URI-scheme casing cannot bypass a peer TLS pin.

HTTPS scheme comparison is case-insensitive under the URI rules used by
``urllib``.  A TLS-paired peer advertised with any legal scheme casing must
therefore be canonicalized to HTTPS and each peer JSON request must either use
its certificate pin or fail before any network request when that pin is absent.
"""

from __future__ import annotations

import json
import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import peer_connectivity


class _JsonResponse:
    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps({"status": "ok"}).encode("utf-8")


class MixedCaseHttpsPinningUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="issue-332-uat-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        with mock.patch.dict(
            os.environ,
            {"USERDATA_ROOT": str(root / "userdata"), "DRONE_DEVICE_ID": "issue-332-local"},
            clear=True,
        ):
            self.settings = Settings.from_env()
        self.pin = root / "victim.crt"

    def test_mixed_case_advertisement_is_a_canonical_tls_candidate(self) -> None:
        peer = {
            "drone_id": "victim-332",
            "reachable_url": "hTtPs://peer.local:8443",
            "certificate_fingerprint": "aa" * 32,
            "paired_over_tls": True,
        }

        self.assertEqual(
            peer_connectivity._peer_address_candidates(peer),
            ["https://peer.local:8443"],
        )
        self.assertEqual(peer_connectivity._peer_address(peer), "https://peer.local:8443")

    def test_mixed_case_https_json_requests_use_the_peer_pin(self) -> None:
        for method in ("get", "post"):
            with self.subTest(method=method), \
                    mock.patch.object(peer_connectivity, "_peer_trust_cafile", return_value=self.pin), \
                    mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True), \
                    mock.patch.object(peer_connectivity.ssl, "create_default_context") as create, \
                    mock.patch.object(peer_connectivity, "urlopen", return_value=_JsonResponse()) as opened:
                url = "hTtPs://peer.local:8443/v1/api/peer/inventory"
                if method == "get":
                    result = peer_connectivity._peer_get_json(url, self.settings, peer_id="victim-332")
                else:
                    result = peer_connectivity._peer_post_json(url, {"probe": True}, self.settings, peer_id="victim-332")

            self.assertEqual(result, {"status": "ok"})
            create.assert_called_once_with(cafile=str(self.pin))
            self.assertIs(opened.call_args.kwargs["context"], create.return_value)

    def test_mixed_case_https_without_a_pin_fails_before_dialing(self) -> None:
        for method in ("get", "post"):
            with self.subTest(method=method), \
                    mock.patch.object(peer_connectivity, "_peer_trust_cafile", return_value=None), \
                    mock.patch.object(peer_connectivity, "urlopen") as opened:
                url = "HTTPS://peer.local:8443/v1/api/peer/inventory"
                with self.assertRaisesRegex(ssl.SSLError, "no trusted certificate"):
                    if method == "get":
                        peer_connectivity._peer_get_json(url, self.settings, peer_id="victim-332")
                    else:
                        peer_connectivity._peer_post_json(url, {}, self.settings, peer_id="victim-332")
                opened.assert_not_called()

    def test_mixed_case_peer_route_selection_pins_before_request(self) -> None:
        peer = {
            "drone_id": "victim-332",
            "scheme": "HTTPS",
            "reachable_url": "hTtPs://peer.local:8443",
            "advertised_reachable_url": "HTTP://attacker.local:8080",
            "certificate_fingerprint": "aa" * 32,
            "paired_over_tls": True,
        }
        with mock.patch.object(peer_connectivity, "_peer_trust_cafile", return_value=self.pin), \
                mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True), \
                mock.patch.object(peer_connectivity, "_candidate_dialable", return_value=True), \
                mock.patch.object(peer_connectivity.ssl, "create_default_context") as create, \
                mock.patch.object(peer_connectivity, "urlopen", return_value=_JsonResponse()) as opened:
            payload, address = peer_connectivity._peer_get_json_for_peer(
                peer, "/v1/api/peer/health", self.settings, peer_id="victim-332",
            )

        self.assertEqual(payload, {"status": "ok"})
        self.assertEqual(address, "https://peer.local:8443")
        create.assert_called_once_with(cafile=str(self.pin))
        self.assertIs(opened.call_args.kwargs["context"], create.return_value)


if __name__ == "__main__":
    unittest.main()
