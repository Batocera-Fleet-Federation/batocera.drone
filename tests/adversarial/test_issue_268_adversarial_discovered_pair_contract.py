"""Issue #268 adversarial contract checks for discovered-peer pairing.

Expected behavior, derived from the issue before reading the implementation:

* ``POST /admin/local-network/peers/{peer_id}/pair`` documents 502 (peer-side failure) and
  503 (this Drone's identity verification holds pairing).
* Every status the discovered-peer route can send is documented, so a client never meets an
  undocumented code.
* The served ``/v1/api/openapi.json`` (what clients read) carries the same contract as the
  module-level spec, and the error responses reference a schema that exists.
* At runtime, through the discovered-peer route itself (not only the shared helper), an
  identity hold is 503 and never 502, a peer HTTP rejection is 502, and an errno-less
  ``PermissionError`` (whatever its text) that is not ``PeerIdentityHold`` is a peer failure (502).
"""

from __future__ import annotations

import inspect
import io
import re
import unittest
from unittest import mock
from urllib.error import HTTPError

from app.drone_api import OPENAPI_SPEC
from app.transfer.peer_connectivity import PAIRING_IDENTITY_HOLD_MESSAGE, PeerIdentityHold
from app.web import handlers_network
from app.web.route_config import api_url

PAIR_PATH = "/admin/local-network/peers/{peer_id}/pair"
FINGERPRINT = "ab" * 32
HOLD_TEXT = PAIRING_IDENTITY_HOLD_MESSAGE


def _pair_responses() -> dict:
    return OPENAPI_SPEC["paths"][PAIR_PATH]["post"]["responses"]


def _emitted_statuses() -> set:
    """Status codes sent by the discovered-peer route and the shared pair helper."""
    sources = [
        handlers_network.HandlersNetworkMixin._handle_admin_local_peer_pair,
        handlers_network.HandlersNetworkMixin._pair_peer_or_reply,
    ]
    codes = set()
    for source in sources:
        codes.update(re.findall(r"_send_json\(\s*(\d{3})", inspect.getsource(source)))
    return {int(code) for code in codes}


class ServedSpecDiscoveredPairTests(unittest.TestCase):
    """The document clients actually fetch must carry the #268 contract."""

    def test_served_openapi_documents_502_and_503_for_discovered_pair(self):
        from app.web.api_app import app

        served = app.openapi()
        operation = served["paths"][api_url(PAIR_PATH)]["post"]

        self.assertIn("502", operation["responses"])
        self.assertIn("503", operation["responses"])

    def test_error_responses_reference_a_schema_that_exists(self):
        for code in ("502", "503"):
            with self.subTest(code=code):
                content = _pair_responses()[code]["content"]["application/json"]["schema"]
                self.assertEqual(content["$ref"].rsplit("/", 1)[-1], "ErrorResponse")
                self.assertIn("ErrorResponse", OPENAPI_SPEC["components"]["schemas"])


class EmittedStatusesAreDocumentedTests(unittest.TestCase):
    """Anything the route can send must appear in its OpenAPI responses."""

    def test_every_emitted_status_is_documented(self):
        emitted = _emitted_statuses()
        documented = {int(code) for code in _pair_responses()}

        self.assertEqual(emitted - {200} - documented, set(), f"undocumented statuses; emitted={sorted(emitted)}")

    def test_scan_is_not_vacuous_for_the_upstream_and_hold_codes(self):
        emitted = _emitted_statuses()

        self.assertIn(502, emitted)
        self.assertIn(503, emitted)


class DiscoveredPairRuntimeTests(unittest.TestCase):
    """Drive the discovered-peer route with a fake discovered row and an injected failure."""

    def setUp(self):
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler._send_json = mock.Mock()
        self.peer = {
            "drone_id": "peer-a",
            "name": "peer-a",
            "reachable_url": "https://peer.example",
            "scheme": "https",
            "certificate_fingerprint": FINGERPRINT,
        }
        local_network = mock.MagicMock()
        local_network.is_local_mode.return_value = True
        local_network.discovered_peers.return_value = [self.peer]
        patches = [
            mock.patch.object(handlers_network, "_local_network", local_network),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run_pair(self, side_effect):
        with mock.patch.object(handlers_network, "_local_pair_peer", side_effect=side_effect):
            self.handler._handle_admin_local_peer_pair("peer-a", {"pairing_code": ""})
        return self.handler._send_json.call_args.args

    def test_identity_hold_is_503_with_the_hold_text(self):
        status, payload = self._run_pair(PeerIdentityHold(HOLD_TEXT))

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], HOLD_TEXT)

    def test_bare_hold_text_permission_error_is_not_an_identity_hold(self):
        # Only PeerIdentityHold is the hold; matching on the text would misreport a
        # stray PermissionError as identity verification holding pairing (issue #403).
        status, payload = self._run_pair(PermissionError(HOLD_TEXT))

        self.assertEqual(status, 502)
        self.assertIn("Could not reach that drone", payload["error"])

    def test_peer_http_rejection_is_502_naming_the_peer_status(self):
        rejected = HTTPError("https://peer.example", 403, "Forbidden", {}, io.BytesIO(b'{"error": "wrong pairing code"}'))
        status, payload = self._run_pair(rejected)

        self.assertEqual(status, 502)
        self.assertIn("HTTP 403", payload["error"])
        self.assertIn("wrong pairing code", payload["error"])

    def test_errno_less_permission_error_outside_the_hold_is_a_peer_failure(self):
        # Once identity is resolved, a PermissionError from the network stack is
        # an unreachable peer (502), not the local hold (503).
        status, payload = self._run_pair(PermissionError("network stack refused"))

        self.assertEqual(status, 502)
        self.assertIn("Could not reach that drone", payload["error"])


if __name__ == "__main__":
    unittest.main()
