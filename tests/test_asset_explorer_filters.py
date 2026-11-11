"""Combined explorer scope against the real SQLite index and browser JS."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from app import drone_api
from app.storage.rom_metadata_store import _empty_rom_metadata_cache, _persist_rom_metadata_cache


class AssetExplorerFilterTests(unittest.TestCase):
    def test_browser_filter_and_chips_runtime(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is needed for browser function checks")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([node, "tests/js/asset_explorer_filters.cjs"], cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_duplicate_filters_share_the_rom_grid_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"USERDATA_ROOT": tmp, "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios"), "DRONE_DEVICE_ID": "explorer-test"}, clear=True):
                settings = drone_api.Settings.from_env()
            entries = {}
            for system in ("snes", "ps3"):
                for region, missing in (("USA", ["image"]), ("Europe", ["image", "video"]), ("Japan", [])):
                    key = f"{system}:{region}.zip"
                    entries[key] = {"system": system, "file_path": f"{region}.zip", "unique_id": key, "rom_name": f"Sonic ({region})", "genre": "Action", "missing_artwork": missing}
            _persist_rom_metadata_cache(settings, {**_empty_rom_metadata_cache(), "entries": entries, "last_full_scan_at": "2026-11-11T00:00:00Z", "scan_in_progress": False}, rom_updates=entries)
            repo = drone_api.RomRepository(settings.roms_root, settings.bios_root, settings=settings)
            handler = drone_api.RomRequestHandler.__new__(drone_api.RomRequestHandler)
            handler.settings, handler.repository = settings, repo
            payloads = []
            handler._send_json = lambda status, payload: payloads.append(payload)
            filters = dict(browser_playable=True, missing_art=["Image", "bogus"], genre="Action", query="Sonic")
            handler._handle_rom_browse(**filters)
            cards = payloads[-1]
            handler._handle_admin_rom_duplicates(**filters)
            groups = payloads[-1]
            self.assertEqual(cards["count"], 2)
            self.assertEqual(groups["count"], 2)
            self.assertEqual({item["unique_id"] for item in groups["groups"][0]["items"]}, {item["unique_id"] for item in cards["roms"]})
            self.assertEqual(groups["genres"], cards["genres"])
            handler._handle_admin_rom_duplicates(**{**filters, "system": "ps3"})
            self.assertEqual(payloads[-1]["groups"], [])
            handler._handle_admin_rom_duplicates(**{**filters, "genre": "RPG"})
            self.assertEqual(payloads[-1]["groups"], [])
            handler._handle_admin_rom_duplicates(**{**filters, "query": "' OR 1=1 --"})
            self.assertEqual(payloads[-1]["groups"], [])
            handler._handle_admin_rom_duplicates(**{**filters, "missing_art": ["video"]})
            self.assertEqual(payloads[-1]["groups"], [])  # one matching copy is not a duplicate


if __name__ == "__main__":
    unittest.main()
