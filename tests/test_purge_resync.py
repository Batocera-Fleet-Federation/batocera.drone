"""End-to-end coverage for the asset-cache purge -> resync flow.

Verifies that after a purge the metadata poller uploads a *full* (replace_all)
inventory to Overmind while reusing cached fingerprint (no re-fingerprint), so the resync
actually clears Overmind's list rather than sending a delta.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
from app.drone_api import (
    RomRepository,
    Settings,
    _hash_rom_metadata_batches,
    _load_rom_metadata_cache,
    _persist_rom_metadata_cache,
    _poll_rom_metadata_cache,
)
from app.storage.rom_metadata_store import (
    _clear_pending_rom_metadata_changes,
    _purge_asset_cache_keep_fingerprint,
)


class PurgeResyncTest(unittest.TestCase):
    def _write_gamelist(self, system: Path, *roms: str) -> None:
        games = "".join(f"<game><path>./{r}</path><name>{r}</name></game>" for r in roms)
        (system / "gamelist.xml").write_text(f"<gameList>{games}</gameList>", encoding="utf-8")

    def _settings_with_rom(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name) / "userdata"
        rom = root / "roms" / "snes" / "Game.zip"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"first")
        self._write_gamelist(rom.parent, "Game.zip")
        with mock.patch.dict(
            "os.environ",
            {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
            clear=True,
        ):
            settings = Settings.from_env()
        return tmp, settings, RomRepository(settings.roms_root, settings.bios_root)

    def test_purge_triggers_full_replace_all_upload_without_rehashing(self) -> None:
        tmp, settings, repo = self._settings_with_rom()
        with tmp:
            # Reach a clean, already-uploaded steady state.
            _poll_rom_metadata_cache(settings, repo)
            with mock.patch("app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", True):
                list(_hash_rom_metadata_batches(settings, repo, batch_size=10))
            cache, _ = _load_rom_metadata_cache(settings)
            cache["dirty"] = False
            cache["full_refresh_pending"] = False
            cache["last_successful_upload_at"] = "2026-06-04T00:00:00+00:00"
            _persist_rom_metadata_cache(settings, cache)

            # The button's action: purge keeping fingerprint.
            _purge_asset_cache_keep_fingerprint(settings)

            posted = []

            def fake_post(url, payload, token=None, settings=None, timeout_seconds=10):
                posted.append({"update_mode": payload.get("update_mode"), "replace_all": payload.get("replace_all")})
                return 200, {"rom_count": len(payload.get("roms") or []), "bios_count": 0, "artwork_count": 0}

            with mock.patch("app.overmind.rom_sync._overmind_post_json_with_status", side_effect=fake_post), mock.patch(
                "app.overmind.rom_sync._load_overmind_config_for_settings",
                return_value={"overmind_url": "https://ov.local", "overmind_token": "tok"},
            ), mock.patch.object(
                RomRepository, "build_fingerprint", side_effect=AssertionError("purge must not re-fingerprint; fingerprint is reused")
            ):
                result = drone_api._poll_rom_metadata_once(settings, repo)

            self.assertEqual(result.get("status"), "uploaded")
            self.assertTrue(posted, "purge did not upload anything to Overmind")
            # The whole inventory must be sent as a full replace, not a delta.
            self.assertTrue(
                all(p["update_mode"] in {"inventory", "inventory_chunk"} for p in posted),
                f"expected a full inventory upload, got {posted}",
            )
            self.assertTrue(any(p.get("replace_all") for p in posted), f"replace_all not set: {posted}")

    def test_steady_state_logs_skip_trigger_and_uploads_nothing(self):
        # With nothing changed, the sync must log decision=skip / reasons=no_changes and
        # post nothing — the observability behind "why did a sync fire?".
        tmp, settings, repo = self._settings_with_rom()
        with tmp:
            drone_api._ASSET_PUSH_REQUESTED.clear()
            _poll_rom_metadata_cache(settings, repo)
            with mock.patch("app.drone_api.ROM_METADATA_HASH_ROMS_ENABLED", True):
                list(_hash_rom_metadata_batches(settings, repo, batch_size=10))
            cache, _ = _load_rom_metadata_cache(settings)
            cache["dirty"] = False
            cache["full_refresh_pending"] = False
            cache["last_successful_upload_at"] = "2026-06-04T00:00:00+00:00"
            _persist_rom_metadata_cache(settings, cache)
            _clear_pending_rom_metadata_changes(settings)

            posted = []

            def fake_post(url, payload, token=None, settings=None, timeout_seconds=10):
                posted.append(payload.get("update_mode"))
                return 200, {"rom_count": 0, "bios_count": 0, "artwork_count": 0}

            with mock.patch("app.overmind.rom_sync._overmind_post_json_with_status", side_effect=fake_post), mock.patch(
                "app.overmind.rom_sync._load_overmind_config_for_settings",
                return_value={"overmind_url": "https://ov.local", "overmind_token": "tok"},
            ):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    drone_api._poll_rom_metadata_once(settings, repo)
                out = buf.getvalue()

            self.assertIn("Asset metadata sync trigger:", out)
            self.assertIn("decision=skip", out)
            self.assertIn("reasons=no_changes", out)
            self.assertEqual(posted, [], f"steady state must not upload anything, posted={posted}")


if __name__ == "__main__":
    unittest.main()
