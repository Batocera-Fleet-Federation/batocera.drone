"""Adversarial check of the client-visible #268 OpenAPI contract."""

import unittest

from app.web.api_app import app
from app.web.route_config import api_url


class ServedDiscoveredPairErrorContract(unittest.TestCase):
    def test_both_failure_responses_survive_openapi_generation_with_error_schema(self):
        operation = app.openapi()["paths"][
            api_url("/admin/local-network/peers/{peer_id}/pair")
        ]["post"]

        # The gateway failure and local identity hold are distinct, documented
        # outcomes. They must remain JSON ErrorResponse entries in the served
        # document clients actually consume.
        for code in ("502", "503"):
            with self.subTest(code=code):
                response = operation["responses"][code]
                self.assertEqual(
                    response["content"]["application/json"]["schema"]["$ref"],
                    "#/components/schemas/ErrorResponse",
                )
        self.assertIn("ErrorResponse", app.openapi()["components"]["schemas"])


if __name__ == "__main__":
    unittest.main()
