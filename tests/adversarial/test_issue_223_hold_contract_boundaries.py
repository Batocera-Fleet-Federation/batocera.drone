"""Issue #223 adversarial contract: every route that now answers the hold documents it.

``_pair_peer_or_reply`` is shared by the pair-by-address route and the discovered-peer
route (``/admin/local-network/peers/{peer_id}/pair``). The #223 change makes the shared
helper answer 503 for a local identity hold, so the discovered-peer route can now return
503 as well. The OpenAPI contract must list that status there too, not only on the route
the issue names.
"""

from __future__ import annotations

import unittest

from app.drone_api import OPENAPI_SPEC


class HoldStatusContractTests(unittest.TestCase):
    def test_discovered_peer_pair_documents_the_hold_status(self) -> None:
        operation = OPENAPI_SPEC["paths"]["/admin/local-network/peers/{peer_id}/pair"]["post"]

        self.assertIn("503", operation["responses"])

    def test_pair_by_address_documents_the_hold_status_with_a_description(self) -> None:
        operation = OPENAPI_SPEC["paths"]["/admin/local-network/pair-by-address"]["post"]

        self.assertIn("503", operation["responses"])
        # The explanation lives in the operation summary or description; either names the hold.
        explanation = f"{operation.get('summary', '')} {operation.get('description', '')}".lower()
        self.assertIn("identity", explanation)


if __name__ == "__main__":
    unittest.main()
