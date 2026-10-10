"""User-level acceptance tests for issue #121: a system click after a search.

Clicking a system in filtered ROM results must queue only the games matching the
search, unless the user explicitly picks the whole system in the confirmation.
The confirmation must show the same scope, count, and size the queue would use.

Covers the bulk endpoint (search scope reaches the peer inventory, a dry run
previews without enqueuing and matches the real queue) and the confirmation
modal wiring in drone.js.
"""

import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
from app.web import handlers_network


ROOT = Path(__file__).resolve().parents[1]
SEARCH_HITS = [
    {"file_path": "Teenage Mutant Ninja Turtles.zip", "file_size": 100},
    {"file_path": "Teenage Mutant Ninja Turtles IV.zip", "file_size": 250},
]


def _real_enqueue_rom(config, peer, system, relative_path, expected_size=None, **kwargs):
    return {
        "job_id": f"job-{relative_path}",
        "file_type": "ROM",
        "asset_type": "roms",
        "relative_path": relative_path,
        "total_bytes": expected_size,
        "status": "queued",
    }


def _fake_enqueue_local_asset(manager, config, peer, asset_type, item, **kwargs):
    # Mirrors the ROM branch of _enqueue_local_asset: one ROM job per item.
    return [manager.enqueue_rom(
        config, peer, kwargs["default_system"], item["file_path"], expected_size=item["file_size"],
    )]


class BulkDownloadScopeHandlerTest(unittest.TestCase):
    def _handler(self, inventory_items):
        handler = object.__new__(drone_api.RomRequestHandler)
        handler.settings = mock.Mock(device_id="target-a")
        handler.repository = mock.Mock()
        handler._fetch_peer_inventory = mock.Mock(return_value={"items": inventory_items, "total": len(inventory_items)})
        handler._enqueue_local_asset = mock.Mock(side_effect=_fake_enqueue_local_asset)
        handler._send_json = mock.Mock()
        return handler

    def _run(self, handler, manager, payload):
        with mock.patch.object(handlers_network._local_network, "is_local_mode", return_value=True), \
                mock.patch.object(handlers_network._local_network, "get_paired_peer", return_value={"drone_id": "peer-a"}), \
                mock.patch.object(handlers_network, "_get_download_manager", return_value=manager):
            handler._handle_admin_local_sync_bulk(payload)
        return handler._send_json.call_args.args

    def _manager(self):
        manager = mock.Mock()
        manager.enqueue_rom.side_effect = _real_enqueue_rom
        return manager

    def test_system_click_with_search_passes_query_to_peer_inventory(self):
        handler = self._handler(SEARCH_HITS)
        self._run(handler, self._manager(), {
            "peer_id": "peer-a", "asset_type": "roms", "system": "snes", "q": "teenage",
            "include_artwork": False, "include_roms": True,
        })

        for call in handler._fetch_peer_inventory.call_args_list:
            params = call.args[3]
            self.assertIn("system=snes", params)
            self.assertIn("q=teenage", params)

    def test_entire_system_request_without_search_does_not_filter(self):
        handler = self._handler(SEARCH_HITS)
        self._run(handler, self._manager(), {
            "peer_id": "peer-a", "asset_type": "roms", "system": "snes", "q": "",
            "include_artwork": False, "include_roms": True,
        })

        for call in handler._fetch_peer_inventory.call_args_list:
            params = call.args[3]
            self.assertIn("system=snes", params)
            self.assertFalse(any(param.startswith("q=") for param in params))

    def test_dry_run_previews_scope_without_enqueuing_anything(self):
        handler = self._handler(SEARCH_HITS)
        manager = self._manager()
        status_code, payload = self._run(handler, manager, {
            "peer_id": "peer-a", "asset_type": "roms", "system": "snes", "q": "teenage",
            "include_artwork": False, "include_roms": True, "dry_run": True,
        })

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["status"], "preview")
        self.assertEqual(payload["queued_assets"], 2)
        self.assertEqual(payload["queued_bytes"], 350)
        self.assertNotIn("queued_jobs", payload)
        manager.enqueue_rom.assert_not_called()

    def test_dry_run_counts_match_the_real_queue(self):
        preview_handler = self._handler(SEARCH_HITS)
        _, preview = self._run(preview_handler, self._manager(), {
            "peer_id": "peer-a", "asset_type": "roms", "system": "snes", "q": "teenage",
            "include_artwork": False, "include_roms": True, "dry_run": True,
        })

        queue_handler = self._handler(SEARCH_HITS)
        manager = self._manager()
        status_code, queued = self._run(queue_handler, manager, {
            "peer_id": "peer-a", "asset_type": "roms", "system": "snes", "q": "teenage",
            "include_artwork": False, "include_roms": True,
        })

        self.assertEqual(status_code, 202)
        self.assertEqual(manager.enqueue_rom.call_count, 2)
        self.assertEqual(preview["queued_assets"], queued["queued_assets"])
        self.assertEqual(preview["queued_bytes"], queued["queued_bytes"])
        self.assertEqual(preview["skipped_existing"], queued["skipped_existing"])


class BulkDownloadScopeUiSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.js = ROOT.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")

    def _function_body(self, signature: str, next_marker: str) -> str:
        start = self.js.index(signature)
        return self.js[start:self.js.index(next_marker, start)]

    def test_system_button_scopes_download_to_the_active_search(self):
        body = self._function_body("async function copyAllRomsForSystem(", "async function queueLocalBulkCopy(")
        self.assertIn("const q = localPeerAssetContext.query", body)
        self.assertIn("body: localBulkBody({ peerId, type: \"roms\", system, q })", body)
        self.assertIn("Entire ${system} system", body)
        self.assertNotIn("window.confirm", body)

    def test_whole_system_is_only_offered_as_an_explicit_choice(self):
        body = self._function_body("async function copyAllRomsForSystem(", "async function queueLocalBulkCopy(")
        self.assertLess(body.index("if (q) {"), body.index("choices.push({"))
        self.assertIn("Includes games outside your search results.", body)

    def test_bulk_entry_points_confirm_through_the_scope_modal(self):
        all_assets = self._function_body("async function copyAllLocalAssets()", "async function copyAllRomsForSystem(")
        self.assertIn("chooseBulkDownloadScope([{", all_assets)
        self.assertNotIn("window.confirm", all_assets)

    def test_modal_names_the_action_and_previews_with_dry_run(self):
        body = self._function_body("function chooseBulkDownloadScope(", "async function copyAllLocalAssets()")
        self.assertIn('{ ...choice.body, dry_run: true }', body)
        self.assertIn("Download ${count} ${localBulkNoun(choices[selected].body)}", body)
        self.assertIn('confirmBtn.textContent = "Nothing to download";', body)
        self.assertIn("formatBytes(bytes)", body)
        self.assertIn("themed-modal", body)
        self.assertIn("btn-close-white", body)

    def test_modal_and_queue_share_one_request_builder(self):
        body = self._function_body("function localBulkBody(", "function localBulkNoun(")
        self.assertIn("q,", body)
        queue_body = self._function_body("async function queueLocalBulkCopy(", "// The Overmind Integration panel")
        self.assertIn('apiPost("/admin/local-network/sync-bulk", body)', queue_body)


if __name__ == "__main__":
    unittest.main()
