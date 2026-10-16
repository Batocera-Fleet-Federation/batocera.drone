"""Issue #268: discovered-peer pairing documents every peer/hold failure."""

import unittest

from app.drone_api import OPENAPI_SPEC


class DiscoveredPeerPairOpenApiErrors(unittest.TestCase):
    def test_post_contract_includes_typed_gateway_and_identity_hold_errors(self):
        operation = OPENAPI_SPEC["paths"][
            "/admin/local-network/peers/{peer_id}/pair"
        ]["post"]
        responses = operation["responses"]

        # Peer-side pairing failures surface as 502; an identity verification
        # hold surfaces as 503. Both are observable outcomes of this operation.
        for status in ("502", "503"):
            with self.subTest(status=status):
                response = responses[status]
                self.assertEqual(
                    response["content"]["application/json"]["schema"]["$ref"],
                    "#/components/schemas/ErrorResponse",
                )

        self.assertEqual(
            responses["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/LocalPeerPairResponse",
        )
        self.assertTrue(
            {"400", "401", "403", "404", "409", "429", "500"}.issubset(responses)
        )


if __name__ == "__main__":
    unittest.main()
