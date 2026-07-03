"""Verify the Drone OpenAPI contract has typed response schemas for every route."""

import unittest

from fastapi.testclient import TestClient

from app.web.api_app import app
from app.drone_api import OPENAPI_SPEC
from app.web.route_config import api_url


def _json_schema(response: dict):
    return response.get("content", {}).get("application/json", {}).get("schema")


def _ref_name(schema: dict):
    if not isinstance(schema, dict):
        return None
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    for key in ("oneOf", "anyOf", "allOf"):
        for part in schema.get(key, []):
            name = _ref_name(part)
            if name:
                return name
    return None


class DroneOpenApiContractTest(unittest.TestCase):
    def test_representative_routes_have_response_components(self):
        checks = [
            ("/systems", "get", "200", "SystemsResponse"),
            ("/systems/{system}", "get", "200", "RomListResponse"),
            ("/bios", "get", "200", "BiosListResponse"),
            ("/admin/api/status", "get", "200", "ApiAdminStatusResponse"),
            ("/admin/automation", "get", "200", "AutomationStatusResponse"),
            ("/admin/local-network/sync", "post", "202", "LocalSyncResponse"),
            ("/peer/health", "get", "200", "PeerHealthResponse"),
            ("/peer/inventory/{asset_type}", "get", "200", "PeerInventoryEnvelope"),
            ("/peer/rom-manifest/{system}/{relative_path}", "get", "200", "PeerRomManifestResponse"),
        ]
        for path, method, code, schema_name in checks:
            with self.subTest(path=path, method=method):
                op = OPENAPI_SPEC["paths"][path][method]
                self.assertEqual(_ref_name(_json_schema(op["responses"][code])), schema_name)

    def test_all_json_responses_are_named_component_refs(self):
        schemas = OPENAPI_SPEC["components"]["schemas"]
        for path, item in OPENAPI_SPEC["paths"].items():
            for method, op in item.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                for code, response in op.get("responses", {}).items():
                    schema = _json_schema(response)
                    if schema is None:
                        continue
                    with self.subTest(path=path, method=method, code=code):
                        ref = _ref_name(schema)
                        self.assertIsNotNone(ref, f"JSON response is not a named schema: {schema!r}")
                        self.assertIn(ref, schemas, f"{ref} is not registered in components.schemas")

    def test_all_responses_advertise_json_or_actual_media_format(self):
        for path, item in OPENAPI_SPEC["paths"].items():
            for method, op in item.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                for code, response in op.get("responses", {}).items():
                    with self.subTest(path=path, method=method, code=code):
                        if code.startswith("3"):
                            self.assertIn("headers", response)
                            continue
                        content = response.get("content")
                        self.assertIsInstance(content, dict)
                        self.assertTrue(content)
                        for media, media_spec in content.items():
                            self.assertIsInstance(media, str)
                            self.assertIn("schema", media_spec)

    def test_expected_dispatched_routes_are_documented(self):
        expected_paths = {
            "/health",
            "/systems",
            "/systems/{system}",
            "/systems/{system}/roms/{unique_id}",
            "/systems/{system}/roms/{unique_id}/fingerprint",
            "/systems/{system}/images",
            "/systems/{system}/images/{image_ref}",
            "/public/systems/{system}/images/{image_file}",
            "/systems/{system}/videos",
            "/systems/{system}/videos/{unique_id}",
            "/bios",
            "/bios/{unique_id}",
            "/search",
            "/theme/meta",
            "/theme/assets/{path}",
            "/theme/system/{system}",
            "/theme/backgrounds",
            "/theme/logos",
            "/theme/images",
            "/admin/downloads",
            "/admin/downloads/{job_id}/cancel",
            "/admin/downloads/{job_id}/retry",
            "/admin/downloads/pause",
            "/admin/downloads/resume",
            "/admin/downloads/clear",
            "/admin/asset-cache",
            "/admin/asset-cache/purge",
            "/admin/asset-cache/clear-pending",
            "/admin/api/status",
            "/admin/api/certificate",
            "/admin/api/certificate/rotate",
            "/admin/automation",
            "/admin/automation/idle-volume",
            "/admin/artwork/missing",
            "/admin/artwork/launchbox/search",
            "/admin/artwork/launchbox/apply",
            "/admin/artwork/thegamesdb/search",
            "/admin/artwork/thegamesdb/apply",
            "/admin/artwork/mobygames/search",
            "/admin/artwork/mobygames/apply",
            "/admin/artwork/upload",
            "/admin/artwork/gamelist/remove",
            "/admin/artwork/gamelist/update",
            "/admin/artwork/gamelist/remove-missing",
            "/admin/integrations/overmind/status",
            "/admin/integrations/overmind/actions",
            "/admin/integrations/overmind/config",
            "/admin/integrations/overmind/start",
            "/admin/integrations/overmind/claim-ownership",
            "/admin/integrations/overmind/swarm/connect",
            "/admin/integrations/overmind/swarm/disconnect",
            "/admin/network-mode",
            "/admin/local-network/status",
            "/admin/local-network/discover",
            "/admin/local-network/pairing-code/rotate",
            "/admin/local-network/peers/{peer_id}/pair",
            "/admin/local-network/peers/{peer_id}/forget",
            "/admin/local-network/peers/{peer_id}/assets",
            "/admin/local-network/sync",
            "/admin/local-network/sync-bulk",
            "/admin/credentials/update",
            "/admin/configs/{source}",
            "/admin/configs/sources",
            "/admin/emulators",
            "/admin/emulators/file",
            "/peer/pair",
            "/peer/health",
            "/peer/inventory/{asset_type}",
            "/peer/roms/{system}/{relative_path}",
            "/peer/rom-manifest/{system}/{relative_path}",
            "/peer/bios/{relative_path}",
            "/peer/saves/{system}/{relative_path}",
            "/peer/artwork/{system}/{artwork_type}/{rom_path}",
        }
        self.assertFalse(expected_paths - set(OPENAPI_SPEC["paths"]))

    def test_fastapi_bridge_merges_legacy_components(self):
        spec = TestClient(app).get(api_url("/openapi.json")).json()
        self.assertIn("SystemsResponse", spec["components"]["schemas"])
        self.assertIn("PeerInventoryEnvelope", spec["components"]["schemas"])
        self.assertIn("/health", spec["paths"])
        self.assertNotIn(api_url("/health"), spec["paths"])
        schema = spec["paths"][api_url("/systems")]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(_ref_name(schema), "SystemsResponse")


if __name__ == "__main__":
    unittest.main()
