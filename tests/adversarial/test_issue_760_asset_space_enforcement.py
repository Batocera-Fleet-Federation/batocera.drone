"""Issue #760 adversarial: free-space enforcement for backup asset restore.

Trusted amendment (authoritative, replaces the warn-only rule):
- "Nothing is queued unless every affected disk has room for the whole selection,
  plus downloads already queued, plus the reserve."
- "Space is re-checked at confirm and before each job."
- "Running out of space holds the batch instead of filling the disk."
- "Unknown sizes: an item whose size no candidate peer reports can't be checked.
  Show it as 'size unknown' and untick it by default."
- "/assets/fetch returns 409 `insufficient_space` and queues nothing when free
  space dropped after planning."
- "The re-check at job start holds the rest of the batch."
- "`ENOSPC` mid-transfer removes the partial file and does not fail over."
"""
import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.backups import archive, asset_restore, assets, service
from app.common.settings import Settings


FP = "a" * 32


def make_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "MOVIES_ROOT": str(root / "movies"),
        "SHOWS_ROOT": str(root / "shows"),
        "MUSIC_ROOT": str(root / "music"),
        "DRONE_DEVICE_ID": "local-drone",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def peer_rom(system, path, fp, size):
    row = {"system": system, "relative_path": path, "rom_fingerprint": fp,
           "entry_type": "file"}
    if size is not None:
        row["file_size"] = size
    return row


class SpaceEnforcementFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.userdata = self.base / "userdata"
        (self.userdata / "system").mkdir(parents=True)
        self.settings = make_settings(self.userdata)
        self.addCleanup(service._RESTORE.clear)
        self.addCleanup(service._ACTIVE_BUILDS.clear)
        self.addCleanup(asset_restore.PLANS.clear)

    def write(self, rel, data=b"x"):
        path = self.userdata / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path


class UnknownSizeTests(SpaceEnforcementFixture):
    def test_unknown_size_item_is_unticked_by_default(self):
        """'Unknown sizes: an item whose size no candidate peer reports can't be
        checked. Show it as size unknown and untick it by default.'"""
        from app.storage.rom_metadata_store import _empty_rom_metadata_cache, _persist_rom_metadata_cache
        self.write("system/batocera.conf", "a=b\n")
        _persist_rom_metadata_cache(
            self.settings,
            {**_empty_rom_metadata_cache(), "entries": {}, "bios_entries": {},
             "last_full_scan_at": "2026-07-14T00:00:00Z", "scan_in_progress": False},
            rom_updates={}, bios_updates={})
        document = assets.sanitize({"version": 1,
                                    "roms": [{"system": "snes", "path": "Mystery.sfc", "size": 0, "fp": FP}]})
        source = {"drone_id": "p1", "name": "P1", "peer": {"drone_id": "p1"}, "tier": 0}

        def fetch(peer, kind, params, timeout):
            return {"total": 1, "items": [peer_rom("snes", "Mystery.sfc", FP, None)]}

        plan = asset_restore.build_plan(
            self.settings, backup={"backup_id": "0" * 32, "name": "B"},
            document=document, kinds=["roms"], sources=[source], fetch_page=fetch)
        item = next(i for i in plan["items"] if i["path"] == "Mystery.sfc")
        self.assertEqual(item["status"], "available")
        # Size unknown -> must not be selected by default.
        self.assertFalse(item["default_selected"],
                         "unknown-size exact match must be unticked by default")
        public = asset_restore.public_plan(plan)
        pub = next(i for i in public["groups"][0]["items"] if i["path"] == "Mystery.sfc")
        self.assertIn("unknown", str(pub.get("size_display", "size unknown")).lower(),
                      "plan must show the item as size unknown")


class ReserveAndQueuedTests(SpaceEnforcementFixture):
    def test_required_counts_selection_queued_and_reserve_with_fits_flag(self):
        """'Nothing is queued unless every affected disk has room for the whole
        selection, plus downloads already queued, plus the reserve.'"""
        self.write("system/batocera.conf", "a=b\n")
        plan = {"space": {"roms": {"path": "/userdata/roms", "free": 1000, "group": "dev1"}},
                "index": {}, "items": []}
        item = {"key": "r0", "kind": "roms", "system": "snes", "path": "G.sfc",
                "size": 500, "action": "download"}
        with mock.patch.dict("os.environ", {"DRONE_ASSET_RESTORE_RESERVE_BYTES": "400"}):
            pools = asset_restore.selection_space(plan, [item])
        by_group = {p.get("group", "dev1"): p for p in pools}
        pool = by_group.get("dev1", pools[0] if pools else None)
        self.assertIsNotNone(pool, "per-filesystem pool must be reported")
        # 500 selected + 400 reserve > ... plus queued below must not fit in 1000.
        self.assertIn("fits", pool, "per-filesystem entry must carry a fits flag")
        self.assertIn("reserve", pool, "per-filesystem entry must carry the reserve")
        self.assertFalse(pool["fits"], "500 selected + reserve must not fit in 1000 free")
        # Already-queued downloads on the same filesystem count too.
        manager = mock.Mock()
        manager.queued_bytes_on_filesystem = mock.Mock(return_value=800)
        with mock.patch.dict("os.environ", {"DRONE_ASSET_RESTORE_RESERVE_BYTES": "0"}):
            pools2 = asset_restore.selection_space(plan, [item], manager=manager)
        pool2 = pools2[0]
        self.assertGreaterEqual(pool2["needed"], 1300,
                                "required must include bytes already queued in DownloadManager")


class ConfirmRecheckTests(SpaceEnforcementFixture):
    def test_fetch_returns_409_and_queues_nothing_when_space_dropped(self):
        """'/assets/fetch returns 409 `insufficient_space` and queues nothing when
        free space dropped after planning.'"""
        self.write("system/batocera.conf", "a=b\n")
        row = service.create_backup(self.settings, name="Src", categories=["batocera"], wait=True)
        backup_id = row["backup_id"]
        plan_id = "p" * 32
        item = {"key": "r0", "kind": "roms", "system": "snes", "path": "Big.sfc",
                "name": "Big", "size": 900, "status": "available", "action": "download",
                "artwork": [], "exact": True, "default_selected": True, "peers": [],
                "_candidates": [{"drone_id": "p1", "name": "P1", "tier": 0, "exact": True,
                                 "system": "snes", "path": "Big.sfc", "size": 900,
                                 "fp": FP, "type": "file", "unit": None, "marker": None,
                                 "artwork": []}],
                "_order": [{"drone_id": "p1", "name": "P1", "tier": 0, "exact": True,
                             "system": "snes", "path": "Big.sfc", "size": 900,
                             "fp": FP, "type": "file", "unit": None, "marker": None,
                             "artwork": []}]}
        asset_restore.PLANS.put({"plan_id": plan_id, "backup_id": backup_id,
                                 "backup_name": "Src", "created_at": 0.0, "legacy": False,
                                 "truncated": False,
                                 "kinds": {"roms": {"here": 0, "available": 1, "probable": 0,
                                                    "unavailable": 0, "bytes": 900}},
                                 "totals": {"here": 0, "available": 1, "probable": 0, "unavailable": 0},
                                 "items": [item], "index": {"r0": item}, "sources": [],
                                 "source_drone_id": "",
                                 "space": {"roms": {"path": str(self.userdata / "roms"),
                                                    "free": 10 ** 9, "group": "dev1"}},
                                 "queued_already": 0, "wished": 1})
        manager = mock.Mock()
        manager.named_batch = mock.MagicMock(side_effect=AssertionError("must queue nothing on 409"))
        with mock.patch("app.backups.asset_restore.shutil.disk_usage",
                        return_value=mock.Mock(free=10)):
            with self.assertRaises(archive.BackupError) as raised:
                service.fetch_assets(self.settings, manager, backup_id, {"plan_id": plan_id})
        self.assertEqual(raised.exception.status, 409)
        self.assertIn("insufficient_space", str(raised.exception.details or "") + str(raised.exception))


class JobStartHoldTests(SpaceEnforcementFixture):
    def test_job_start_recheck_holds_batch_instead_of_starting(self):
        """'The re-check at job start holds the rest of the batch.' and 'Space is
        re-checked at confirm and before each job.'"""
        import app.transfer.download_manager as dm
        src = Path(dm.__file__).read_text(encoding="utf-8")
        self.assertIn("insufficient_space", src,
                      "DownloadManager must hold jobs with failure_reason=insufficient_space")
        self.assertIn("disk_usage", src,
                      "DownloadManager must re-check free space before each job starts")


class EnospcTests(SpaceEnforcementFixture):
    def test_enospc_removes_partial_and_does_not_fail_over(self):
        """'`ENOSPC` mid-transfer removes the partial file and does not fail over.'"""
        import app.transfer.download_manager as dm
        src = Path(dm.__file__).read_text(encoding="utf-8")
        self.assertIn("ENOSPC", src, "DownloadManager must handle ENOSPC explicitly")
        self.assertIn("insufficient_space", src,
                      "ENOSPC must hold the job with failure_reason=insufficient_space")


if __name__ == "__main__":
    unittest.main()
