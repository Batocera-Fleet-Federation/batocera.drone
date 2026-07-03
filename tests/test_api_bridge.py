"""Tests for the Drone FastAPI bridge: the typed /v1/api surface, the merged OpenAPI spec,
the route-ownership routing, and the safe-by-default (flag off) behaviour.
"""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from app.web import api_app, api_bridge
from app.web.api_app import OWNED_EXACT, OWNED_PREFIXES, app
from app.web.route_config import API_PREFIX, api_url


class ApiAppTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_api_info_is_typed(self):
        body = self.client.get(api_url("/api-info")).json()
        self.assertEqual(body["api_prefix"], API_PREFIX)
        self.assertTrue(body["fastapi_bridge"])
        self.assertEqual(body["openapi_url"], api_url("/openapi.json"))
        self.assertIn(api_url("/api-info"), body["migrated_paths"])

    def test_openapi_documents_migrated_route_with_response_schema(self):
        spec = self.client.get(api_url("/openapi.json")).json()
        op = spec["paths"][api_url("/api-info")]["get"]
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        self.assertEqual(ref.rsplit("/", 1)[-1], "ApiInfoResponse")
        self.assertIn("ApiInfoResponse", spec["components"]["schemas"])

    def test_openapi_merges_legacy_handwritten_paths(self):
        # During phasing the spec must still document endpoints not yet on FastAPI.
        spec = self.client.get(api_url("/openapi.json")).json()
        self.assertTrue(
            any(p.startswith(api_url("/admin")) for p in spec["paths"]),
            "legacy OPENAPI_SPEC paths were not merged into the FastAPI spec",
        )

    def test_swagger_redirects_to_docs(self):
        resp = self.client.get(api_url("/swagger"), follow_redirects=False)
        self.assertEqual(resp.status_code, 307)
        self.assertEqual(resp.headers["location"], api_url("/docs"))

    def test_overmind_status_route_serves_service_payload(self):
        payload = {
            "overmind_url": "https://overmind.example",
            "machine_id": "drone-xyz",
            "status": {"integration_state": "polling", "integration_enabled": True},
            "swarm": [],
            "overmind_active": True,
        }
        self.addCleanup(api_app.set_settings, None)
        api_app.set_settings(SimpleNamespace(admin_enabled=True))
        with mock.patch("app.drone_api.build_overmind_status", return_value=payload):
            body = self.client.get(api_url("/admin/integrations/overmind/status")).json()
        self.assertEqual(body["machine_id"], "drone-xyz")
        self.assertEqual(body["status"]["integration_state"], "polling")

    def test_overmind_status_respects_admin_disabled(self):
        self.addCleanup(api_app.set_settings, None)
        api_app.set_settings(SimpleNamespace(admin_enabled=False))
        resp = self.client.get(api_url("/admin/integrations/overmind/status"))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {"error": "admin disabled"})

    def test_openapi_documents_overmind_status(self):
        spec = self.client.get(api_url("/openapi.json")).json()
        op = spec["paths"][api_url("/admin/integrations/overmind/status")]["get"]
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        self.assertEqual(ref.rsplit("/", 1)[-1], "OvermindStatusResponse")


class BridgeRoutingTest(unittest.TestCase):
    def _bridge(self):
        return api_bridge._Bridge(port=0, owned_exact=set(OWNED_EXACT), owned_prefixes=OWNED_PREFIXES)

    def test_owns_migrated_paths(self):
        bridge = self._bridge()
        for path in ("/openapi.json", "/api-info", "/swagger", "/docs", "/docs/oauth2-redirect",
                     "/admin/integrations/overmind/status"):
            self.assertTrue(bridge.owns(path), path)

    def test_does_not_own_legacy_or_peer_paths(self):
        bridge = self._bridge()
        for path in ("/systems", "/admin/overmind/status", "/peer/health", "/peer/roms/x/y"):
            self.assertFalse(bridge.owns(path), path)

    def test_bridge_is_inactive_by_default(self):
        # No DRONE_API_FASTAPI_BRIDGE=1 -> maybe_start must be a no-op so the device stays
        # on 100% stdlib dispatch.
        api_bridge._BRIDGE = None
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(api_bridge.maybe_start())
        self.assertIsNone(api_bridge.active())


if __name__ == "__main__":
    unittest.main()
