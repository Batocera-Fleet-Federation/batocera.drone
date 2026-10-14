"""Out-of-scope adversarial finding for issue #223: discovered-peer pairing omits 502 from OpenAPI.

``/admin/local-network/peers/{peer_id}/pair`` answers 502 for a peer HTTP rejection and for an
unreachable peer, through the same ``_pair_peer_or_reply`` helper as pair-by-address. Its
OpenAPI ``error_codes`` list omits 502, while pair-by-address documents it. The gap predates
#223 (the helper's 502 is unchanged by this issue) and is not about the identity hold, so
it is reported here as out of scope. The 503 that #223 adds to this route is covered by the
blocking ``test_issue_223_hold_contract_boundaries.py``.

The assertion pins the current contract as evidence and is kept in its own suite.
"""

from __future__ import annotations

import unittest

from app.drone_api import OPENAPI_SPEC


class DiscoveredPairDocumentsBadGatewayTests(unittest.TestCase):
    def test_discovered_peer_pair_omits_the_502_it_returns(self) -> None:
        """Observed: the route documents 400/401/403/404/409/429/500 but not the 502 it returns."""
        responses = OPENAPI_SPEC["paths"]["/admin/local-network/peers/{peer_id}/pair"]["post"]["responses"]

        self.assertNotIn("502", responses)

    def test_pair_by_address_documents_the_same_502(self) -> None:
        responses = OPENAPI_SPEC["paths"]["/admin/local-network/pair-by-address"]["post"]["responses"]

        self.assertIn("502", responses)


if __name__ == "__main__":
    unittest.main()
