"""Regression coverage: discovered-peer pairing documents its 502 response.

``/admin/local-network/peers/{peer_id}/pair`` answers 502 for a peer HTTP rejection and for an
unreachable peer, through the same ``_pair_peer_or_reply`` helper as pair-by-address. Its
OpenAPI response list must document 502, just as pair-by-address does. The operation also
documents 503 for the identity hold; that behavior is covered by the blocking
``test_issue_223_hold_contract_boundaries.py`` and the dedicated #268 contract test.
"""

from __future__ import annotations

import unittest

from app.drone_api import OPENAPI_SPEC


class DiscoveredPairDocumentsBadGatewayTests(unittest.TestCase):
    def test_discovered_peer_pair_documents_the_502_it_returns(self) -> None:
        responses = OPENAPI_SPEC["paths"]["/admin/local-network/peers/{peer_id}/pair"]["post"]["responses"]

        self.assertIn("502", responses)

    def test_pair_by_address_documents_the_same_502(self) -> None:
        responses = OPENAPI_SPEC["paths"]["/admin/local-network/pair-by-address"]["post"]["responses"]

        self.assertIn("502", responses)


if __name__ == "__main__":
    unittest.main()
