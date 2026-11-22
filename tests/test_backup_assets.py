"""Issue #760: backups record their games/media and can fetch what is missing from the swarm."""
import gzip
import hashlib
import io
import json
import tarfile
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from app.backups import archive, asset_restore, assets, service
from app.common.settings import Settings
from app.device import notifications
from app.drone_api import RomRepository
from app.storage import music_store
from app.storage.rom_metadata_store import _empty_rom_metadata_cache, _persist_rom_metadata_cache
from app.transfer import local_network
from app.transfer.download_manager import DownloadManager

BACKUP_ID = "0123456789abcdef0123456789abcdef"
FP_MARIO = "a" * 32
FP_ZELDA = "b" * 32
FP_METROID_PEER = "c" * 32
FP_METROID_BACKUP = "d" * 32
FP_DK = "e" * 32
FP_KIRBY = "f" * 32
MD5_BIOS = "1" * 32


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


def rom_entry(system, path, fingerprint, size=100, gamelist=True, missing=()):
    entry = {"system": system, "file_path": path, "rom_name": Path(path).stem, "file_size": size,
             "fingerprint": fingerprint, "missing_artwork": list(missing)}
    if gamelist:
        entry["gamelist_path"] = f"{system}/gamelist.xml"
    return entry


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.userdata = self.base / "userdata"
        self.userdata.mkdir()
        self.settings = make_settings(self.userdata)
        self.addCleanup(service._RESTORE.clear)
        self.addCleanup(service._ACTIVE_BUILDS.clear)
        self.addCleanup(asset_restore.PLANS.clear)

    def write(self, rel, data=b"x"):
        path = self.userdata / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path

    def seed_rom_cache(self, entries, bios=None):
        rows = {f"{e['system']}:{e['file_path']}": e for e in entries}
        bios_rows = {f"bios:{b['file_path']}": b for b in (bios or [])}
        _persist_rom_metadata_cache(
            self.settings,
            {**_empty_rom_metadata_cache(), "entries": rows, "bios_entries": bios_rows,
             "last_full_scan_at": "2026-07-14T00:00:00Z", "scan_in_progress": False},
            rom_updates=rows, bios_updates=bios_rows)


class AssetRecordTests(Fixture):
    def setUp(self):
        super().setUp()
        self.write("system/batocera.conf", "global.shaderset=crt\n")
        self.write("roms/snes/Mario.sfc", "mario")
        self.write("music/Artist/Album/01 Song.mp3", "song")
        self.seed_rom_cache(
            [rom_entry("snes", "Mario.sfc", FP_MARIO, 5, missing=("video", "manual")),
             {**rom_entry("gc", "Metroid/Metroid.m3u", FP_KIRBY, 900, gamelist=False),
              "entry_type": "folder", "transfer_unit_path": "Metroid", "marker_relative_path": "Metroid/Metroid.m3u"}],
            bios=[{"file_path": "scph1001.bin", "name": "scph1001.bin", "file_size": 512, "md5": MD5_BIOS}])
        music_store.sync_music_cache(self.settings.music_root)

    def build(self, kind="backup", **options):
        return service.create_backup(self.settings, name="A", kind=kind, categories=["batocera"], wait=True, **options)

    def test_settings_only_backup_records_the_games_and_media(self):
        row = self.build()
        self.assertEqual(row["status"], "complete")
        manifest = service.local_manifest(self.settings, row["backup_id"])
        recorded = manifest["assets"]
        self.assertEqual(recorded["counts"], {"roms": 2, "bios": 1, "movies": 0, "music": 1})
        self.assertEqual(recorded["media_roms"], 1)
        document = service.load_assets(self.settings, row["backup_id"])
        self.assertFalse(document["legacy"])
        mario = next(item for item in document["roms"] if item["path"] == "Mario.sfc")
        self.assertEqual((mario["system"], mario["fp"], mario["size"]), ("snes", FP_MARIO, 5))
        # The cache holds no media fields for ROMs without a gamelist entry; the video and manual tags
        # are empty, so only the other six were recorded.
        self.assertEqual(mario["media"], ["image", "thumbnail", "marquee", "fanart", "boxart", "wheel"])
        folder = next(item for item in document["roms"] if item["system"] == "gc")
        self.assertEqual((folder["type"], folder["unit"], folder["marker"]), ("folder", "Metroid", "Metroid/Metroid.m3u"))
        self.assertNotIn("media", folder)
        self.assertEqual(document["bios"], [{"path": "scph1001.bin", "size": 512, "md5": MD5_BIOS}])
        self.assertEqual(document["music"][0]["path"], "Artist/Album/01 Song.mp3")
        overview = service.detail(self.settings, row["backup_id"])["assets"]
        self.assertTrue(overview["recorded"])
        self.assertEqual(overview["counts"]["roms"], 2)
        # Peers and the sidecar see only the small summary, never the entries.
        self.assertNotIn("Mario.sfc", json.dumps(service.peer_manifest(self.settings, row["backup_id"])))
        self.assertTrue(service.assets_path(self.settings, row["backup_id"]).is_file())

    def test_snapshots_do_not_record_assets(self):
        row = self.build(kind="snapshot")
        manifest = service.local_manifest(self.settings, row["backup_id"])
        self.assertNotIn("assets", manifest)
        self.assertFalse(service.assets_path(self.settings, row["backup_id"]).exists())
        with tarfile.open(service.archive_path(self.settings, row["backup_id"])) as tar:
            self.assertNotIn(archive.ASSETS_MEMBER, tar.getnames())

    def test_archive_verifies_and_member_must_match_the_manifest(self):
        row = self.build()
        path = service.archive_path(self.settings, row["backup_id"])
        carried = []
        archive.verify_archive(path, expected_id=row["backup_id"], assets_out=carried)
        self.assertEqual(hashlib_sha(carried[0]), service.local_manifest(self.settings, row["backup_id"])["assets"]["sha256"])

        def rewritten(mutate):
            out = self.base / "tampered.tar.gz"
            with tarfile.open(path, "r|gz") as source, tarfile.open(out, "w:gz") as target:
                for member in source:
                    data = source.extractfile(member).read()
                    member, data = mutate(member, data)
                    if member is None:
                        continue
                    member.size = len(data)
                    target.addfile(member, io.BytesIO(data))
            return out

        def swap_assets(member, data):
            if member.name == archive.ASSETS_MEMBER:
                return member, assets.encode(assets.sanitize({"version": 1, "roms": [{"system": "snes", "path": "x.sfc"}]}))
            return member, data

        with self.assertRaisesRegex(archive.BackupError, "asset record"):
            archive.verify_archive(rewritten(swap_assets), expected_id=row["backup_id"])

        def drop_assets(member, data):
            return (None, data) if member.name == archive.ASSETS_MEMBER else (member, data)

        with self.assertRaisesRegex(archive.BackupError, "asset record"):
            archive.verify_archive(rewritten(drop_assets), expected_id=row["backup_id"])

    def test_old_manifests_without_assets_still_validate_and_fall_back_to_gamelists(self):
        gamelist = ('<gameList><game><path>./Mario.sfc</path><name>Mario</name><image>./images/m.png</image>'
                    '<video>./videos/m.mp4</video></game><game><path>./sub/Zelda.sfc</path><name>Zelda</name></game></gameList>')
        self.write("roms/snes/gamelist.xml", gamelist)
        row = service.create_backup(self.settings, name="old", categories=["gamelists"], wait=True)
        # Simulate a backup made before #760: no summary, no member, no sidecar.
        path = service.archive_path(self.settings, row["backup_id"])
        stripped = self.base / "old.tar.gz"
        with tarfile.open(path, "r|gz") as source, tarfile.open(stripped, "w:gz") as target:
            for member in source:
                data = source.extractfile(member).read()
                if member.name == archive.ASSETS_MEMBER:
                    continue
                if member.name == archive.MANIFEST_MEMBER:
                    manifest = json.loads(data)
                    manifest.pop("assets")
                    data = json.dumps(manifest).encode()
                member.size = len(data)
                target.addfile(member, io.BytesIO(data))
        stripped.replace(path)
        service.assets_path(self.settings, row["backup_id"]).unlink()
        manifest = service.local_manifest(self.settings, row["backup_id"])
        manifest.pop("assets", None)
        archive.write_sidecar(service.manifest_path(self.settings, row["backup_id"]), manifest)
        archive.verify_archive(path, expected_id=row["backup_id"])

        self.assertEqual(service.detail(self.settings, row["backup_id"])["assets"],
                         {"recorded": False, "legacy": True, "counts": {k: 0 for k in assets.KINDS},
                          "bytes": {k: 0 for k in assets.KINDS}, "truncated": False, "media_roms": 0})
        document = service.load_assets(self.settings, row["backup_id"])
        self.assertTrue(document["legacy"])
        self.assertEqual([(i["system"], i["path"], i["size"], "fp" in i, i.get("media")) for i in document["roms"]],
                         [("snes", "Mario.sfc", 0, False, ["image", "video"]), ("snes", "sub/Zelda.sfc", 0, False, None)])


def hashlib_sha(blob):
    return hashlib.sha256(blob).hexdigest()


class HostileAssetTests(unittest.TestCase):
    def test_sanitize_drops_unsafe_entries_and_caps_lists(self):
        good = {"system": "snes", "path": "Game.sfc", "size": 10, "fp": FP_MARIO, "media": ["image", "evil", "image"]}
        raw = {"version": 1, "roms": [
            good,
            {"system": "snes", "path": "/etc/passwd"},
            {"system": "snes", "path": "../../etc/passwd"},
            {"system": "snes", "path": "a/../b"},
            {"system": "snes", "path": "a\\b"},
            {"system": "snes", "path": "a\x00b"},
            {"system": "snes", "path": "C:/Windows"},
            {"system": "../x", "path": "a.sfc"},
            {"system": "sn es/x", "path": "a.sfc"},
            {"system": "snes", "path": "b.sfc", "fp": "not-hex"},
            {"system": "snes", "path": "c.sfc", "size": -5, "type": "<script>"},
            "nonsense", None, {"system": "snes", "path": "game.sfc"},
        ], "bios": [{"path": "ok.bin", "md5": "G" * 32}, {"path": "../bad.bin"}], "movies": "nope", "music": [{"path": ""}]}
        clean = assets.sanitize(raw)
        self.assertEqual(clean["roms"][0], {"system": "snes", "path": "Game.sfc", "type": "file", "size": 10, "fp": FP_MARIO,
                                            "media": ["image"]})
        paths = [item["path"] for item in clean["roms"]]
        self.assertEqual(paths, ["Game.sfc", "b.sfc", "c.sfc"])  # duplicate (case-insensitive) dropped
        self.assertNotIn("fp", clean["roms"][1])
        self.assertEqual((clean["roms"][2]["size"], clean["roms"][2]["type"]), (0, "file"))
        self.assertEqual(clean["bios"], [{"path": "ok.bin", "size": 0}])
        self.assertEqual((clean["movies"], clean["music"]), ([], []))

    def test_oversize_lists_are_capped_and_flagged(self):
        with mock.patch.object(assets, "MAX_ENTRIES_PER_KIND", 3):
            clean = assets.sanitize({"version": 1, "music": [{"path": f"{i}.mp3"} for i in range(10)]})
        self.assertEqual(len(clean["music"]), 3)
        self.assertTrue(clean["truncated"])

    def test_unknown_versions_and_bombs_are_refused(self):
        with self.assertRaises(ValueError):
            assets.sanitize({"version": 2})
        with self.assertRaises(ValueError):
            assets.decode(b"not gzip")
        bomb = gzip.compress(b'{"version":1,"pad":"' + b"a" * 4096 + b'"}')
        with mock.patch.object(assets, "MAX_JSON_BYTES", 1024), self.assertRaisesRegex(ValueError, "too large"):
            assets.decode(bomb)
        with self.assertRaises(ValueError):
            assets.decode(gzip.compress(b"[1, 2]"))

    def test_manifest_summary_is_sanitized(self):
        self.assertIsNone(assets.clean_summary({"version": 1, "sha256": "zz", "size": 5}))
        self.assertIsNone(assets.clean_summary({"version": 9, "sha256": "a" * 64, "size": 5}))
        self.assertIsNone(assets.clean_summary({"version": 1, "sha256": "a" * 64, "size": assets.MAX_MEMBER_BYTES + 1}))
        summary = assets.clean_summary({"version": 1, "sha256": "a" * 64, "size": 5, "counts": {"roms": 10 ** 12, "x": 1},
                                        "bytes": {"roms": "lots"}, "truncated": "yes"})
        self.assertEqual(summary["counts"]["roms"], assets.MAX_ENTRIES_PER_KIND)
        self.assertEqual(summary["bytes"]["roms"], 0)
        self.assertFalse(summary["truncated"])
        self.assertNotIn("x", summary["counts"])
        manifest = {"format": archive.FORMAT, "version": 1, "backup_id": BACKUP_ID, "files": [], "assets": summary}
        self.assertEqual(archive.validate_manifest(manifest)["assets"], summary)
        manifest["kind"] = "snapshot"
        self.assertNotIn("assets", archive.validate_manifest(manifest))
        manifest["kind"], manifest["assets"] = "backup", {"version": 1, "sha256": "bad", "size": 1}
        self.assertNotIn("assets", archive.validate_manifest(manifest))

    def test_legacy_gamelists_reject_doctype_and_unsafe_paths(self):
        def member(name, data):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            return info, io.BytesIO(data)

        bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
                b"<gameList><game><path>./Bomb.sfc</path><name>&b;</name></game></gameList>")
        bomb_utf16 = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<!DOCTYPE lolz [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
            '<gameList><game><path>./Bomb16.sfc</path><name>&b;</name></game></gameList>'
        ).encode("utf-16")
        bomb_latin1 = (
            '<?xml version="1.0" encoding="ISO-8859-1"?>'
            '<!DOCTYPE lolz [<!ENTITY a "aaaa">]>'
            '<gameList><game><path>./BombL1.sfc</path><name>&a;</name></game></gameList>'
        ).encode("iso-8859-1")
        valid_utf16 = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<gameList><game><path>./Clean16.sfc</path><name>Clean &amp; Safe</name></game></gameList>'
        ).encode("utf-16")
        evil = (b"<gameList><game><path>/etc/passwd</path></game><game><path>../up.sfc</path></game>"
                b"<game><path>./Fine.sfc</path><name>Fine</name><image>./i.png</image></game></gameList>")
        members = [member("data/roms/snes/gamelist.xml", bomb),
                   member("data/roms/nes/gamelist.xml", bomb_utf16),
                   member("data/roms/md/gamelist.xml", bomb_latin1),
                   member("data/roms/gb/gamelist.xml", valid_utf16),
                   member("data/roms/gba/gamelist.xml", evil),
                   member("data/roms/../etc/gamelist.xml", evil), member("data/roms/psx/sub/gamelist.xml", evil),
                   member("data/roms/n64/gamelist.xml", b"<gameList><broken")]
        document = assets.legacy_from_gamelists(members)
        self.assertEqual([(i["system"], i["path"]) for i in document["roms"]],
                         [("gb", "Clean16.sfc"), ("gba", "Fine.sfc")])


class PlanFixture(Fixture):
    """A Drone that has Mario and a music track, asking two online peers for the rest."""

    def setUp(self):
        super().setUp()
        self.write("roms/snes/Mario.sfc", "mario")
        self.write("music/Artist/Have.mp3", "have")
        self.seed_rom_cache([rom_entry("snes", "Mario.sfc", FP_MARIO, 5)],
                            bios=[{"file_path": "have.bin", "name": "have.bin", "file_size": 4, "md5": "2" * 32}])
        music_store.sync_music_cache(self.settings.music_root)
        self.pages = {}
        self.calls = []
        self.broken = {}
        for peer_id in ("p1", "p2"):
            local_network.save_paired_peer(self.settings, {"drone_id": peer_id, "name": peer_id.upper(), "scheme": "https",
                                                           "address": f"{peer_id}.example", "api_port": 443})
        self.sources = [{"drone_id": pid, "name": pid.upper(), "peer": local_network.get_paired_peer(self.settings, pid), "tier": 0}
                        for pid in ("p1", "p2")]
        patcher = mock.patch("app.transfer.asset_enqueue._ensure_rom_write_access", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self, peer, kind, *rows):
        self.pages[(peer, kind)] = list(rows)

    def fetch(self, source, kind, params, timeout):
        self.calls.append((source["drone_id"], kind, dict(params)))
        error = self.broken.get(source["drone_id"])
        if error:
            raise error
        rows = self.pages.get((source["drone_id"], kind), [])
        offset, limit = int(params["offset"]), int(params["limit"])
        return {"total": len(rows), "items": rows[offset:offset + limit]}

    def document(self, **kinds):
        return assets.sanitize({"version": 1, **kinds})

    def plan(self, document, kinds=("roms", "movies", "music", "bios"), sources=None, manager=None, repository=None):
        return asset_restore.build_plan(
            self.settings, backup={"backup_id": BACKUP_ID, "name": "Big", "source": {"drone_id": "p1"}}, document=document,
            kinds=kinds, sources=self.sources if sources is None else sources, fetch_page=self.fetch, manager=manager,
            repository=repository)

    def item(self, plan, name):
        return next(item for item in plan["items"] if item["path"] == name)


def peer_rom(system, path, fingerprint, size=100, **extra):
    return {"system": system, "relative_path": path, "file_size": size, "rom_fingerprint": fingerprint,
            "entry_type": "file", **extra}


class PlanTests(PlanFixture):
    def test_classifies_here_available_probable_and_unavailable(self):
        document = self.document(roms=[
            {"system": "snes", "path": "Mario.sfc", "size": 5, "fp": FP_MARIO},
            {"system": "snes", "path": "Zelda.sfc", "size": 200, "fp": FP_ZELDA},
            {"system": "snes", "path": "Metroid.sfc", "size": 300, "fp": FP_METROID_BACKUP},
            {"system": "snes", "path": "Kirby.sfc", "size": 400, "fp": FP_KIRBY},
            {"system": "snes", "path": "Donkey.sfc", "size": 500, "fp": FP_DK},
            {"system": "snes", "path": "NameOnly.sfc", "size": 600, "fp": "9" * 32},
            {"system": "gba", "path": "Legacy.gba", "size": 0},
        ], bios=[{"path": "have.bin", "size": 4, "md5": "2" * 32}, {"path": "new.bin", "size": 9, "md5": MD5_BIOS}],
            music=[{"path": "Artist/Have.mp3", "size": 4}, {"path": "Artist/New.mp3", "size": 7, "fp": "7" * 32}])
        self.rows("p1", "roms",
                  peer_rom("snes", "Zelda.sfc", FP_ZELDA, 200), peer_rom("snes", "Metroid.sfc", FP_METROID_PEER, 300),
                  peer_rom("snes", "renamed/DK Country.sfc", FP_DK, 500), peer_rom("snes", "other/NameOnly.sfc", "8" * 32, 600),
                  peer_rom("gba", "Legacy.gba", FP_KIRBY, 1234), peer_rom("snes", "Mario.sfc", FP_MARIO, 5))
        self.rows("p2", "roms", peer_rom("snes", "Zelda.sfc", FP_ZELDA, 200))
        self.rows("p2", "bios", {"relative_path": "new.bin", "file_size": 9, "bios_md5": MD5_BIOS})
        self.rows("p1", "music", {"file_path": "Artist/New.mp3", "file_size": 7, "music_fingerprint": "7" * 32})
        plan = self.plan(document)

        self.assertEqual(plan["totals"], {"here": 3, "available": 4, "probable": 2, "unavailable": 2})
        zelda = self.item(plan, "Zelda.sfc")
        self.assertEqual((zelda["status"], sorted(p["drone_id"] for p in zelda["peers"]), zelda["default_selected"]),
                         ("available", ["p1", "p2"], True))
        metroid = self.item(plan, "Metroid.sfc")
        self.assertEqual((metroid["status"], metroid["default_selected"], metroid["peers"][0]["exact"]),
                         ("probable", False, False))
        # A fingerprint match beats the path: the file is wanted under a different name on the peer.
        donkey = self.item(plan, "Donkey.sfc")
        self.assertEqual(donkey["status"], "available")
        self.assertEqual(donkey["_candidates"][0]["path"], "renamed/DK Country.sfc")
        # Same file name in another folder is a name-only match: never offered.
        self.assertEqual(self.item(plan, "NameOnly.sfc")["status"], "unavailable")
        self.assertEqual(self.item(plan, "Kirby.sfc")["status"], "unavailable")
        # An older backup (no size, no fingerprint) can only ever be a probable path match.
        self.assertEqual(self.item(plan, "Legacy.gba")["status"], "probable")
        self.assertEqual(self.item(plan, "new.bin")["status"], "available")
        self.assertEqual(self.item(plan, "Artist/New.mp3")["status"], "available")
        self.assertFalse(any(item["path"] in ("Mario.sfc", "have.bin", "Artist/Have.mp3") for item in plan["items"]))
        public = asset_restore.public_plan(plan)
        self.assertEqual([item["path"] for item in public["probable"]], ["Metroid.sfc", "Legacy.gba"])
        self.assertEqual([item["path"] for item in public["unavailable"]], ["Kirby.sfc", "NameOnly.sfc"])
        self.assertNotIn("_candidates", json.dumps(public))
        self.assertEqual({s["drone_id"]: (s["status"], s["matched"]) for s in public["sources"]},
                         {"p1": ("ok", 5), "p2": ("ok", 2)})

    def test_pending_downloads_are_excluded(self):
        manager = mock.Mock()
        manager.find_pending_asset.side_effect = lambda kind, path, **kw: {"id": "j"} if path == "Zelda.sfc" else None
        manager.peer_tier.return_value = 0
        document = self.document(roms=[{"system": "snes", "path": "Zelda.sfc", "size": 1, "fp": FP_ZELDA},
                                       {"system": "snes", "path": "Kirby.sfc", "size": 1, "fp": FP_KIRBY}])
        self.rows("p1", "roms", peer_rom("snes", "Zelda.sfc", FP_ZELDA, 1), peer_rom("snes", "Kirby.sfc", FP_KIRBY, 1))
        plan = self.plan(document, manager=manager)
        self.assertEqual(plan["totals"], {"here": 1, "available": 1, "probable": 0, "unavailable": 0})
        self.assertEqual(plan["queued_already"], 1)

    def test_a_slow_old_or_offline_peer_never_fails_the_plan(self):
        import socket
        from urllib.error import HTTPError, URLError
        self.broken = {"p1": socket.timeout("slow"), "p2": HTTPError("u", 404, "nf", {}, None)}
        document = self.document(roms=[{"system": "snes", "path": "Zelda.sfc", "size": 1, "fp": FP_ZELDA}])
        plan = self.plan(document)
        self.assertEqual({s["drone_id"]: s["status"] for s in plan["sources"]}, {"p1": "timeout", "p2": "unsupported"})
        self.assertEqual(plan["totals"]["unavailable"], 1)
        self.broken = {"p1": URLError("down"), "p2": None}
        self.rows("p2", "roms", peer_rom("snes", "Zelda.sfc", FP_ZELDA, 1))
        plan = self.plan(document)
        self.assertEqual({s["drone_id"]: s["status"] for s in plan["sources"]}, {"p1": "offline", "p2": "ok"})
        self.assertEqual(plan["totals"]["available"], 1)

    def test_pages_are_walked_scoped_to_the_backup_systems_and_bounded(self):
        document = self.document(roms=[{"system": "snes", "path": "Z.sfc", "size": 1, "fp": FP_ZELDA},
                                       {"system": "gba", "path": "Y.gba", "size": 1, "fp": FP_KIRBY}])
        self.rows("p1", "roms", *[peer_rom("snes", f"Filler{i}.sfc", f"{i:032x}", 1) for i in range(2500)],
                  peer_rom("snes", "Z.sfc", FP_ZELDA, 1))
        plan = self.plan(document, kinds=("roms",), sources=self.sources[:1])
        self.assertEqual(plan["totals"]["available"], 1)
        calls = [call for call in self.calls if call[0] == "p1"]
        self.assertEqual([call[2]["offset"] for call in calls], [0, 1000, 2000])
        self.assertTrue(all(call[2]["systems"] == "gba,snes" and call[2]["limit"] == 1000 for call in calls))
        with mock.patch.object(asset_restore, "MAX_PAGES_PER_KIND", 1):
            plan = self.plan(document, kinds=("roms",), sources=self.sources[:1])
        self.assertEqual(plan["totals"]["unavailable"], 2)

    def test_peer_rows_with_hostile_paths_are_never_candidates(self):
        document = self.document(roms=[{"system": "snes", "path": "Zelda.sfc", "size": 1, "fp": FP_ZELDA}])
        self.rows("p1", "roms", {**peer_rom("snes", "../../etc/passwd", FP_ZELDA, 1)},
                  {**peer_rom("snes", "/abs/Zelda.sfc", FP_ZELDA, 1)},
                  {**peer_rom("../snes", "Zelda.sfc", FP_ZELDA, 1)})
        plan = self.plan(document)
        self.assertEqual(plan["totals"]["unavailable"], 1)

    def test_free_space_is_checked_per_filesystem_with_reserve_queued_and_enforced(self):
        document = self.document(roms=[{"system": "snes", "path": "Zelda.sfc", "size": 5000, "fp": FP_ZELDA}],
                                 music=[{"path": "Artist/New.mp3", "size": 700, "fp": "7" * 32}],
                                 bios=[{"path": "new.bin", "size": 200, "md5": MD5_BIOS}])
        self.rows("p1", "roms", peer_rom("snes", "Zelda.sfc", FP_ZELDA, 5000))
        self.rows("p1", "music", {"file_path": "Artist/New.mp3", "file_size": 700, "fingerprint": "7" * 32})
        self.rows("p1", "bios", {"relative_path": "new.bin", "file_size": 200, "bios_md5": MD5_BIOS})
        usage = mock.Mock(free=1000, total=10000)

        def fake_stat(path):
            st = mock.Mock()
            st.st_dev = 2 if "bios" in str(path) else 1
            return st

        with mock.patch("app.backups.asset_restore.shutil.disk_usage", return_value=usage), \
                mock.patch("app.transfer.disk_space.os.stat", side_effect=fake_stat), \
                mock.patch.dict("os.environ", {"DRONE_ASSET_RESTORE_RESERVE_BYTES": "200"}):
            plan = self.plan(document, kinds=("roms", "music", "bios"))
            items = asset_restore.resolve_selection(plan, {})
            space = asset_restore.selection_space(plan, items)
            self.assertEqual(len(space), 2)  # roms+music share st_dev=1; bios is on st_dev=2
            by_group = {p["group"]: p for p in space}
            self.assertEqual((by_group["1"]["selected"], by_group["1"]["reserve"], by_group["1"]["required"], by_group["1"]["fits"]),
                             (5700, 200, 5900, False))
            self.assertEqual((by_group["2"]["selected"], by_group["2"]["reserve"], by_group["2"]["required"], by_group["2"]["fits"]),
                             (200, 200, 400, True))
            only_music = asset_restore.resolve_selection(plan, {"deselected": [self.item(plan, "Zelda.sfc")["key"]]})
            self.assertTrue(asset_restore.selection_space(plan, only_music)[0]["fits"])
            # Already-queued bytes on the same filesystem count toward required space.
            mgr = mock.Mock()
            mgr.queued_bytes_on_filesystem.return_value = 250
            with_queued = asset_restore.selection_space(plan, only_music, manager=mgr)
            by_q = {p["group"]: p for p in with_queued}
            self.assertEqual(by_q["1"]["required"], 700 + 250 + 200)
            self.assertFalse(by_q["1"]["fits"])

    def test_unknown_size_items_are_unticked_by_default(self):
        document = self.document(roms=[{"system": "snes", "path": "Mystery.sfc", "size": 0, "fp": FP_ZELDA}])
        self.rows("p1", "roms", {"system": "snes", "relative_path": "Mystery.sfc", "rom_fingerprint": FP_ZELDA, "entry_type": "file"})
        plan = self.plan(document, kinds=("roms",))
        item = self.item(plan, "Mystery.sfc")
        self.assertEqual(item["status"], "available")
        self.assertFalse(item["default_selected"])
        self.assertTrue(item["size_unknown"])
        self.assertEqual(item["size_display"], "size unknown")
        self.assertEqual(asset_restore.resolve_selection(plan, {}), [])
        pub = asset_restore.public_plan(plan)
        self.assertEqual(pub["groups"][0]["default_count"], 0)
        self.assertEqual(pub["groups"][0]["items"][0]["size_display"], "size unknown")

    def test_probable_items_need_a_tick_and_selection_is_confined_to_the_plan(self):
        document = self.document(roms=[{"system": "snes", "path": "Metroid.sfc", "size": 300, "fp": FP_METROID_BACKUP},
                                       {"system": "snes", "path": "Zelda.sfc", "size": 200, "fp": FP_ZELDA},
                                       {"system": "snes", "path": "Kirby.sfc", "size": 1, "fp": FP_KIRBY}])
        self.rows("p1", "roms", peer_rom("snes", "Zelda.sfc", FP_ZELDA, 200), peer_rom("snes", "Metroid.sfc", FP_METROID_PEER, 300))
        plan = self.plan(document)
        keys = {item["path"]: item["key"] for item in plan["items"]}
        self.assertEqual([i["path"] for i in asset_restore.resolve_selection(plan, {})], ["Zelda.sfc"])
        ticked = asset_restore.resolve_selection(plan, {"selected": [keys["Metroid.sfc"]]})
        self.assertEqual(sorted(i["path"] for i in ticked), ["Metroid.sfc", "Zelda.sfc"])
        self.assertEqual(asset_restore.resolve_selection(plan, {"deselected_groups": ["roms:snes"]}), [])
        with self.assertRaises(archive.BackupError):
            asset_restore.resolve_selection(plan, {"selected": ["r999"]})
        with self.assertRaisesRegex(archive.BackupError, "no online Drone"):
            asset_restore.resolve_selection(plan, {"selected": [keys["Kirby.sfc"]]})
        with self.assertRaises(archive.BackupError):
            asset_restore.resolve_selection(plan, {"selected": "r1"})

    def test_plan_cache_expires_and_belongs_to_one_backup(self):
        plan = self.plan(self.document())
        self.assertIs(asset_restore.PLANS.get(plan["plan_id"], BACKUP_ID), plan)
        with self.assertRaises(archive.BackupError):
            asset_restore.PLANS.get(plan["plan_id"], "f" * 32)
        clock = [0.0]
        store = asset_restore.PlanStore(ttl=10, limit=2, clock=lambda: clock[0])
        store.put(plan)
        clock[0] = 11
        with self.assertRaises(archive.BackupError) as raised:
            store.get(plan["plan_id"])
        self.assertEqual(raised.exception.status, 409)

    def test_an_unresolved_identity_means_swarm_unavailable(self):
        with mock.patch("app.backups.asset_restore._identity.swarm_networking_allowed", return_value=False):
            with self.assertRaises(asset_restore.SwarmUnavailable) as raised:
                self.plan(self.document())
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(self.calls, [])

    def test_artwork_is_offered_only_for_missing_media(self):
        self.write("roms/snes/images/Mario-image.png", "png")
        gamelist = ET.Element("gameList")
        game = ET.SubElement(gamelist, "game")
        ET.SubElement(game, "path").text = "./Mario.sfc"
        ET.SubElement(game, "image").text = "./images/Mario-image.png"
        ET.SubElement(game, "video").text = "./videos/gone.mp4"
        ET.ElementTree(gamelist).write(self.userdata / "roms/snes/gamelist.xml")
        repository = RomRepository(self.settings.roms_root, self.settings.bios_root, settings=self.settings)
        document = self.document(roms=[{"system": "snes", "path": "Mario.sfc", "size": 5, "fp": FP_MARIO,
                                        "media": ["image", "video", "manual"]}])
        self.rows("p1", "roms", peer_rom("snes", "Mario.sfc", FP_MARIO, 5,
                                         gamelist={"image": "./i.png", "video": "./v.mp4", "manual": "./m.pdf"}))
        plan = self.plan(document, kinds=("roms",), repository=repository)
        item = self.item(plan, "Mario.sfc")
        # The ROM is here and its image exists; the missing video and manual can come from the peer.
        self.assertEqual((item["status"], item["action"], item["artwork"]), ("here", "artwork", ["manual", "video"]))
        self.assertTrue(item["default_selected"])
        self.assertEqual(plan["totals"]["here"], 1)


class AssignmentTests(unittest.TestCase):
    @staticmethod
    def item(key, size, *peers):
        candidates = [{"drone_id": pid, "tier": tier, "exact": True, "name": pid, "artwork": []} for pid, tier in peers]
        return {"key": key, "size": size, "_candidates": candidates}

    def test_files_are_spread_across_peers_in_the_best_tier_by_assigned_bytes(self):
        items = [self.item(f"r{i}", size, ("a", 0), ("b", 0), ("c", 1)) for i, size in enumerate((900, 800, 100, 100, 100, 100))]
        load = asset_restore.assign_sources(items)
        firsts = [item["_order"][0]["drone_id"] for item in items]
        self.assertNotIn("c", firsts)  # direct-public only when no LAN/tailnet peer has the file
        self.assertEqual(sorted(set(firsts)), ["a", "b"])
        self.assertLessEqual(abs(load["a"] - load["b"]), 900)
        self.assertEqual(sum(load.values()), 2100)
        # Failover order: remaining candidates, best tier first.
        self.assertEqual([c["drone_id"] for c in items[0]["_order"]][-1], "c")

    def test_only_peer_with_the_file_in_a_worse_tier_is_still_used(self):
        items = [self.item("r1", 5, ("far", 1))]
        asset_restore.assign_sources(items)
        self.assertEqual(items[0]["_order"][0]["drone_id"], "far")

    def test_source_drone_is_only_a_tie_breaker_within_a_tier(self):
        tied = self.item("r1", 10, ("b", 0), ("source", 0))
        asset_restore.assign_sources([tied], "source")
        self.assertEqual(tied["_order"][0]["drone_id"], "source")
        worse_tier = self.item("r2", 10, ("lan", 0), ("source", 1))
        asset_restore.assign_sources([worse_tier], "source")
        self.assertEqual(worse_tier["_order"][0]["drone_id"], "lan")
        loaded = [self.item("r3", 1000, ("source", 0)), self.item("r4", 10, ("other", 0), ("source", 0))]
        asset_restore.assign_sources(loaded, "source")
        self.assertEqual(loaded[1]["_order"][0]["drone_id"], "other")

    def test_exact_candidates_are_preferred_over_probable_ones(self):
        item = self.item("r1", 10, ("a", 0), ("b", 0))
        item["_candidates"][0]["exact"] = False
        asset_restore.assign_sources([item])
        self.assertEqual([c["drone_id"] for c in item["_order"]], ["b"])


def make_manager(root: Path) -> DownloadManager:
    with mock.patch("app.transfer.download_manager.Thread.start"):
        settings = make_settings(root)
        return DownloadManager(settings, RomRepository(settings.roms_root, settings.bios_root, settings=settings))


class FetchFixture(PlanFixture):
    def setUp(self):
        super().setUp()
        self.manager = make_manager(self.userdata)
        self.events = []
        patcher = mock.patch("app.transfer.download_manager._notifications.record_event",
                             side_effect=lambda settings, kind, title, message, details=None: self.events.append((kind, message, details)))
        patcher.start()
        self.addCleanup(patcher.stop)
        audit = mock.patch("app.transfer.download_manager._notifications.record_audit_event")
        audit.start()
        self.addCleanup(audit.stop)
        self.manager._lan_transport.lan_url = lambda peer: "https://lan" if peer.get("drone_id") in self.lan else None
        self.lan = {"p1", "p2"}

    def fetch_items(self, plan, selection=None):
        items = asset_restore.resolve_selection(plan, selection or {})
        return asset_restore.fetch_plan(self.settings, self.manager, plan, items, batch_id="b" * 32, label="Big")

    def jobs(self):
        return list(self.manager._jobs.values())

    def complete(self, job_id, fetch=None):
        def ok(request, context):
            return {"status": "completed", "relative_path": request.relative_path, "asset_type": request.asset_type,
                    "downloaded_bytes": 10, "total_bytes": 10}
        self.run_job(job_id, fetch or ok)

    def run_job(self, job_id, fetch):
        with self.manager._lock:
            job = self.manager._jobs[job_id]
            job["status"] = "downloading"
            job["_started_mono"] = time.monotonic()
        with mock.patch.object(self.manager._selector, "fetch", side_effect=fetch), \
                mock.patch("app.transfer.download_manager._kick_asset_metadata_sync_after_download"):
            self.manager._run_job(job_id)


class FetchTests(FetchFixture):
    def test_downloads_are_spread_across_peers_with_one_batch_and_per_file_expectations(self):
        document = self.document(roms=[{"system": "snes", "path": f"G{i}.sfc", "size": 100 * (i + 1), "fp": f"{i + 1:032x}"}
                                       for i in range(6)])
        for peer in ("p1", "p2"):
            self.rows(peer, "roms", *[peer_rom("snes", f"G{i}.sfc", f"{i + 1:032x}", 100 * (i + 1)) for i in range(6)])
        plan = self.plan(document, kinds=("roms",))
        result = self.fetch_items(plan)
        self.assertEqual((result["queued"], result["skipped"], result["batch_id"]), (6, 0, "b" * 32))
        jobs = self.jobs()
        self.assertEqual({job["source_drone_id"] for job in jobs}, {"p1", "p2"})
        by_peer = {pid: sum(job["file_size"] for job in jobs if job["source_drone_id"] == pid) for pid in ("p1", "p2")}
        self.assertLessEqual(abs(by_peer["p1"] - by_peer["p2"]), 600)
        self.assertTrue(all(job["_batch_id"] == "b" * 32 and job["_overwrite"] is False for job in jobs))
        self.assertTrue(all(job["_expected_fingerprint"] == f"{int(job['file_path'][1]) + 1:032x}" for job in jobs))
        self.assertEqual([e[0] for e in self.events], ["asset_transfer_started"])
        self.assertIn('Restoring 6 games from backup "Big"', self.events[0][1])

    def test_one_finished_entry_across_several_peers(self):
        document = self.document(roms=[{"system": "snes", "path": f"G{i}.sfc", "size": 10, "fp": f"{i + 1:032x}"} for i in range(4)],
                                 roms_unavailable=None)
        for peer in ("p1", "p2"):
            self.rows(peer, "roms", *[peer_rom("snes", f"G{i}.sfc", f"{i + 1:032x}", 10) for i in range(4)])
        plan = self.plan(document, kinds=("roms",))
        self.fetch_items(plan)
        for job in self.jobs():
            self.complete(job["job_id"])
        kinds = [event[0] for event in self.events]
        self.assertEqual(kinds, ["asset_transfer_started", "asset_transfer_finished"])
        self.assertIn("4 completed, 0 failed", self.events[1][1])
        self.assertEqual(self.events[1][2]["batch_id"], "b" * 32)

    def test_unavailable_items_are_reported_in_the_batch_and_never_queued(self):
        document = self.document(roms=[{"system": "snes", "path": "Zelda.sfc", "size": 10, "fp": FP_ZELDA},
                                       {"system": "snes", "path": "Kirby.sfc", "size": 10, "fp": FP_KIRBY}])
        self.rows("p1", "roms", peer_rom("snes", "Zelda.sfc", FP_ZELDA, 10))
        plan = self.plan(document, kinds=("roms",))
        result = self.fetch_items(plan)
        self.assertEqual((result["queued"], result["unavailable"]), (1, 1))
        self.complete(self.jobs()[0]["job_id"])
        self.assertIn("1 unavailable on the swarm", self.events[-1][1])
        self.assertEqual(self.events[-1][2]["unavailable"], 1)

    def test_an_existing_rom_is_never_overwritten_and_gets_no_download(self):
        self.rows("p1", "roms", peer_rom("snes", "Mario.sfc", "9" * 32, 5))
        document = self.document(roms=[{"system": "snes", "path": "Mario.sfc", "size": 5, "fp": FP_MARIO}])
        plan = self.plan(document, kinds=("roms",))
        self.assertEqual(plan["totals"]["here"], 1)
        self.assertEqual(asset_restore.resolve_selection(plan, {}), [])
        # Even a stale plan that wanted it again would be refused by the enqueue rules.
        item = {"key": "r0", "kind": "roms", "system": "snes", "path": "Mario.sfc", "size": 5, "action": "download",
                "artwork": [], "_candidates": [{"drone_id": "p1", "name": "P1", "tier": 0, "exact": True, "system": "snes",
                                                "path": "Mario.sfc", "size": 5, "fp": FP_MARIO, "type": "file", "unit": None,
                                                "marker": None, "artwork": []}]}
        queued = asset_restore.fetch_plan(self.settings, self.manager, {**plan, "totals": plan["totals"]}, [item])
        self.assertEqual((queued["queued"], queued["skipped"]), (0, 1))
        self.assertEqual((self.userdata / "roms/snes/Mario.sfc").read_text(), "mario")

    def test_folder_unit_roms_pull_the_folder_but_keep_the_marker_identity(self):
        document = self.document(roms=[{"system": "gc", "path": "Metroid/Metroid.m3u", "type": "folder", "unit": "Metroid",
                                        "marker": "Metroid/Metroid.m3u", "size": 900, "fp": FP_KIRBY}])
        self.rows("p1", "roms", peer_rom("gc", "Metroid/Metroid.m3u", FP_KIRBY, 900, entry_type="folder",
                                         transfer_unit_path="Metroid", marker_relative_path="Metroid/Metroid.m3u"))
        plan = self.plan(document, kinds=("roms",))
        self.fetch_items(plan)
        job = self.jobs()[0]
        self.assertEqual((job["file_path"], job["entry_type"], job["_marker_relative_path"]),
                         ("Metroid", "folder", "Metroid/Metroid.m3u"))

    def test_bios_is_opt_in_and_verified_by_the_peers_md5(self):
        document = self.document(bios=[{"path": "new.bin", "size": 9, "md5": MD5_BIOS}],
                                 roms=[{"system": "snes", "path": "Zelda.sfc", "size": 1, "fp": FP_ZELDA}])
        self.rows("p1", "bios", {"relative_path": "new.bin", "file_size": 9, "bios_md5": MD5_BIOS})
        self.rows("p1", "roms", peer_rom("snes", "Zelda.sfc", FP_ZELDA, 1))
        plan = self.plan(document, kinds=("roms",))
        self.assertEqual({item["kind"] for item in plan["items"]}, {"roms"})
        self.assertEqual({c[1] for c in self.calls}, {"roms"})
        plan = self.plan(document, kinds=("roms", "bios"))
        self.fetch_items(plan)
        bios = next(job for job in self.jobs() if job["file_type"] == "BIOS")
        self.assertEqual((bios["_expected_fingerprint"], bios["bios_md5"], bios["_overwrite"]), (MD5_BIOS, MD5_BIOS, False))
        self.assertEqual(service._clean_asset_kinds(None, True), ["roms", "bios", "movies", "music"])
        self.assertEqual(service._clean_asset_kinds(None, False), ["roms", "movies", "music"])
        with self.assertRaises(archive.BackupError):
            service._clean_asset_kinds(["saves"], False)

    def test_movies_and_music_queue_with_the_peers_fingerprint(self):
        document = self.document(music=[{"path": "Artist/New.mp3", "size": 7, "fp": "7" * 32}],
                                 movies=[{"path": "Film.mkv", "size": 70, "fp": "6" * 32}])
        self.rows("p1", "music", {"file_path": "Artist/New.mp3", "file_size": 7, "music_fingerprint": "5" * 32,
                                  "fingerprint": "5" * 32})
        self.rows("p2", "movies", {"file_path": "Film.mkv", "file_size": 70, "movies_fingerprint": "6" * 32})
        plan = self.plan(document, kinds=("music", "movies"))
        self.assertEqual(self.item(plan, "Artist/New.mp3")["status"], "probable")
        self.fetch_items(plan, {"selected": [self.item(plan, "Artist/New.mp3")["key"]]})
        by_type = {job["file_type"]: job for job in self.jobs()}
        # The expected fingerprint is what the *peer* reports, never the backup's.
        self.assertEqual(by_type["Music"]["_expected_fingerprint"], "5" * 32)
        self.assertEqual((by_type["Movie"]["source_drone_id"], by_type["Movie"]["_expected_fingerprint"]), ("p2", "6" * 32))
        self.assertTrue(all(job["_overwrite"] is False for job in self.jobs()))

    def test_artwork_never_overwrites_and_only_missing_fields_are_queued(self):
        self.write("roms/snes/images/Mario-image.png", "png")
        self.write("roms/snes/gamelist.xml", "<gameList><game><path>./Mario.sfc</path><name>Mario</name>"
                                              "<desc>Restored text</desc><image>./images/Mario-image.png</image></game></gameList>")
        self.rows("p1", "roms", peer_rom("snes", "Mario.sfc", FP_MARIO, 5, gamelist={"image": "./i.png", "video": "./v.mp4"}))
        document = self.document(roms=[{"system": "snes", "path": "Mario.sfc", "size": 5, "fp": FP_MARIO, "media": ["image", "video"]}])
        repository = self.manager.repository
        plan = self.plan(document, kinds=("roms",), repository=repository)
        self.fetch_items(plan)
        jobs = self.jobs()
        self.assertEqual([(job["file_type"], job["artwork_type"], job["_overwrite"]) for job in jobs], [("ARTWORK", "video", False)])
        self.assertEqual(jobs[0]["_local_rom_path"], "Mario.sfc")

    def test_artwork_download_keeps_restored_text_fields(self):
        self.write("roms/snes/gamelist.xml", "<gameList><game><path>./Mario.sfc</path><name>Restored</name>"
                                              "<desc>My notes</desc><rating>0.9</rating><playcount>12</playcount>"
                                              "<favorite>true</favorite></game></gameList>")
        repository = self.manager.repository
        repository.update_gamelist_artwork_reference("snes", "Mario.sfc", "video", "videos/Mario-video.mp4")
        game = ET.parse(self.userdata / "roms/snes/gamelist.xml").getroot().find("game")
        self.assertEqual({child.tag: child.text for child in game},
                         {"path": "./Mario.sfc", "name": "Restored", "desc": "My notes", "rating": "0.9", "playcount": "12",
                          "favorite": "true", "video": "./videos/Mario-video.mp4"})


class FailoverTests(FetchFixture):
    def queue_zelda(self, peers=("p1", "p2"), lan=None):
        self.lan = set(lan if lan is not None else peers)
        document = self.document(roms=[{"system": "snes", "path": "Zelda.sfc", "size": 10, "fp": FP_ZELDA}])
        for peer in peers:
            self.rows(peer, "roms", peer_rom("snes", "Zelda.sfc" if peer == "p1" else "alt/Zelda.sfc", FP_ZELDA if peer == "p1" else "0" * 31 + "1", 10))
        # Both copies answer to the backup's fingerprint at plan time; p2 also holds a differing copy.
        if "p2" in peers:
            self.rows("p2", "roms", peer_rom("snes", "alt/Zelda.sfc", FP_ZELDA, 10))
        plan = self.plan(document, kinds=("roms",), sources=[s for s in self.sources if s["drone_id"] in peers])
        self.fetch_items(plan)
        return self.jobs()[0]

    def ok(self, request, context):
        return {"status": "completed", "relative_path": request.relative_path, "asset_type": "rom", "downloaded_bytes": 10, "total_bytes": 10}

    def test_a_failed_peer_hands_the_job_to_the_next_peer_that_has_it(self):
        job = self.queue_zelda()
        first = job["source_drone_id"]
        seen = []

        def fetch(request, context):
            seen.append((context.peer["drone_id"], request.relative_path))
            if len(seen) == 1:
                raise OSError("connection reset")
            return self.ok(request, context)

        with mock.patch("app.transfer.download_manager._local_network.get_paired_peer",
                        side_effect=lambda settings, pid: {"drone_id": pid, "name": pid}):
            self.run_job(job["job_id"], fetch)
            self.assertEqual(self.manager._jobs[job["job_id"]]["status"], "queued")
            self.run_job(job["job_id"], fetch)
        final = self.manager._jobs[job["job_id"]]
        self.assertEqual(final["status"], "completed")
        self.assertNotEqual(seen[0][0], seen[1][0])
        self.assertEqual(seen[0][0], first)
        # Each attempt used that peer's own path for the file.
        self.assertEqual(dict(seen), {"p1": "Zelda.sfc", "p2": "alt/Zelda.sfc"})
        self.assertEqual(final["failover_count"], 1)
        self.assertEqual(final["failover_failed_sources"][0]["drone_id"], first)

    def test_a_fingerprint_mismatch_from_one_peer_moves_to_the_next(self):
        job = self.queue_zelda()
        calls = []

        def fetch(request, context):
            calls.append(context.peer["drone_id"])
            if len(calls) == 1:
                raise ValueError("ROM fingerprint mismatch: expected x got y")
            return self.ok(request, context)

        with mock.patch("app.transfer.download_manager._local_network.get_paired_peer",
                        side_effect=lambda settings, pid: {"drone_id": pid, "name": pid}):
            self.run_job(job["job_id"], fetch)
            self.run_job(job["job_id"], fetch)
        self.assertEqual(self.manager._jobs[job["job_id"]]["status"], "completed")
        self.assertEqual(len(set(calls)), 2)

    def test_the_job_fails_only_when_no_peers_are_left(self):
        job = self.queue_zelda()

        def fetch(request, context):
            raise ValueError("ROM fingerprint mismatch")

        with mock.patch("app.transfer.download_manager._local_network.get_paired_peer",
                        side_effect=lambda settings, pid: {"drone_id": pid, "name": pid}):
            self.run_job(job["job_id"], fetch)
            self.assertEqual(self.manager._jobs[job["job_id"]]["status"], "queued")
            self.run_job(job["job_id"], fetch)
        self.assertEqual(self.manager._jobs[job["job_id"]]["status"], "failed")
        self.assertEqual(self.events[-1][0], "asset_transfer_finished")
        self.assertIn("0 completed, 1 failed", self.events[-1][1])

    def test_network_errors_on_the_last_peer_retry_a_few_times_then_fail(self):
        job = self.queue_zelda(peers=("p1",))

        def fetch(request, context):
            raise OSError("down")

        for _ in range(asset_failover_retries() + 1):
            job_now = self.manager._jobs[job["job_id"]]
            job_now["reconnect_after_epoch"] = 0
            self.run_job(job["job_id"], fetch)
        self.assertEqual(self.manager._jobs[job["job_id"]]["status"], "failed")

    def test_cancel_and_identity_hold_never_fail_over(self):
        from app.transfer.download_errors import DownloadCancelled
        from app.transfer.peer_connectivity import PeerIdentityHold
        job = self.queue_zelda()
        with mock.patch("app.transfer.download_manager._local_network.get_paired_peer",
                        side_effect=lambda settings, pid: {"drone_id": pid, "name": pid}):
            self.run_job(job["job_id"], mock.Mock(side_effect=DownloadCancelled("cancelled")))
            self.assertEqual(self.manager._jobs[job["job_id"]]["status"], "cancelled")
        job = self.queue_zelda()
        remaining = len(job["_failover"])
        first = job["source_drone_id"]
        with mock.patch("app.transfer.download_manager._local_network.get_paired_peer",
                        side_effect=lambda settings, pid: {"drone_id": pid, "name": pid}):
            self.run_job(job["job_id"], mock.Mock(side_effect=PeerIdentityHold("verifying identity")))
        held = self.manager._jobs[job["job_id"]]
        self.assertEqual((held["source_drone_id"], len(held["_failover"])), (first, remaining))
        self.assertNotIn("failover_count", held)

    def test_cancel_batch_cancels_queued_jobs_and_pending_retries(self):
        document = self.document(roms=[{"system": "snes", "path": f"G{i}.sfc", "size": 10, "fp": f"{i + 1:032x}"} for i in range(3)])
        for peer in ("p1", "p2"):
            self.rows(peer, "roms", *[peer_rom("snes", f"G{i}.sfc", f"{i + 1:032x}", 10) for i in range(3)])
        plan = self.plan(document, kinds=("roms",))
        self.fetch_items(plan)
        first = self.jobs()[0]["job_id"]
        with mock.patch("app.transfer.download_manager._local_network.get_paired_peer",
                        side_effect=lambda settings, pid: {"drone_id": pid, "name": pid}):
            self.run_job(first, mock.Mock(side_effect=OSError("boom")))  # fails over -> back in the queue
        self.assertEqual(self.manager._jobs[first]["status"], "queued")
        other = self.manager.enqueue_rom({}, {"drone_id": "p1"}, "snes", "Unrelated.sfc", expected_size=1)
        result = self.manager.cancel_batch("b" * 32)
        self.assertEqual((result["status"], result["cancelled"]), ("cancelled", 3))
        self.assertTrue(all(job["status"] == "cancelled" for job in self.jobs() if job.get("_batch_id") == "b" * 32))
        self.assertEqual(self.manager._jobs[other["job_id"]]["status"], "queued")
        self.assertEqual(self.events[-1][0], "asset_transfer_finished")
        self.assertIn("3 cancelled", self.events[-1][1])
        self.assertEqual(self.manager.cancel_batch("nope")["status"], "not_found")

    def test_failover_overrides_are_whitelisted(self):
        job = self.queue_zelda()
        with self.manager._lock:
            self.manager._jobs[job["job_id"]]["_failover"] = [{"drone_id": "p2", "set": {
                "relative_path": "alt/Zelda.sfc", "_config": {"x": 1}, "status": "completed", "_overwrite": True}}]
        with mock.patch("app.transfer.download_manager._local_network.get_paired_peer",
                        side_effect=lambda settings, pid: {"drone_id": pid, "name": pid}):
            self.run_job(job["job_id"], mock.Mock(side_effect=OSError("x")))
        moved = self.manager._jobs[job["job_id"]]
        self.assertEqual((moved["relative_path"], moved["status"], moved["_overwrite"]), ("alt/Zelda.sfc", "queued", False))
        self.assertNotIn("x", moved["_config"])

    def test_job_start_space_recheck_holds_batch_without_failover(self):
        document = self.document(roms=[{"system": "snes", "path": f"G{i}.sfc", "size": 500, "fp": f"{i + 1:032x}"} for i in range(2)])
        for peer in ("p1", "p2"):
            self.rows(peer, "roms", *[peer_rom("snes", f"G{i}.sfc", f"{i + 1:032x}", 500) for i in range(2)])
        plan = self.plan(document, kinds=("roms",))
        with mock.patch.dict("os.environ", {"DRONE_ASSET_RESTORE_RESERVE_BYTES": "0"}):
            self.fetch_items(plan)
        jobs = self.jobs()
        fetch = mock.Mock(side_effect=AssertionError("must not start download when space is short"))
        with mock.patch("app.transfer.download_manager.shutil.disk_usage", return_value=mock.Mock(free=200, total=2000)), \
                mock.patch.dict("os.environ", {"DRONE_ASSET_RESTORE_RESERVE_BYTES": "100"}):
            self.run_job(jobs[0]["job_id"], fetch)
        fetch.assert_not_called()
        for job in self.jobs():
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["failure_reason"], "insufficient_space")
        self.assertEqual(self.events[-1][0], "asset_transfer_finished")
        self.assertIn("insufficient disk space", self.events[-1][1])
        self.assertGreaterEqual(self.events[-1][2]["space_needed_bytes"], 1100)

    def test_enospc_removes_partial_and_holds_batch_without_failover(self):
        import errno
        document = self.document(roms=[{"system": "snes", "path": f"G{i}.sfc", "size": 500, "fp": f"{i + 1:032x}"} for i in range(2)])
        for peer in ("p1", "p2"):
            self.rows(peer, "roms", *[peer_rom("snes", f"G{i}.sfc", f"{i + 1:032x}", 500) for i in range(2)])
        plan = self.plan(document, kinds=("roms",))
        with mock.patch.dict("os.environ", {"DRONE_ASSET_RESTORE_RESERVE_BYTES": "0"}):
            self.fetch_items(plan)
        jobs = self.jobs()
        first_job = jobs[0]
        part = self.userdata / "roms" / "snes" / f"{first_job['relative_path']}.part"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"partial-bytes")

        def fail_enospc(request, context):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.dict("os.environ", {"DRONE_ASSET_RESTORE_RESERVE_BYTES": "0"}):
            self.run_job(first_job["job_id"], fail_enospc)
        self.assertFalse(part.exists(), "ENOSPC must remove leftover partial file")
        for job in self.jobs():
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["failure_reason"], "insufficient_space")
        self.assertNotIn("failover_count", self.manager._jobs[first_job["job_id"]])


def asset_failover_retries():
    from app.transfer import download_manager
    return download_manager.FAILOVER_LAST_SOURCE_RETRIES


class RestoreFlowTests(PlanFixture):
    def setUp(self):
        super().setUp()
        self.write("system/batocera.conf", "global.shaderset=crt\n")
        self.manager = mock.Mock()
        self.manager.repository = None
        self.manager.find_pending_asset.return_value = None
        self.manager.peer_tier.return_value = 0
        self.row = service.create_backup(self.settings, name="Source", categories=["batocera"], wait=True)

    def make_plan(self):
        self.rows("p1", "roms", peer_rom("snes", "Zelda.sfc", FP_ZELDA, 10))
        document = self.document(roms=[{"system": "snes", "path": "Zelda.sfc", "size": 10, "fp": FP_ZELDA}])
        plan = asset_restore.build_plan(self.settings, backup={"backup_id": self.row["backup_id"], "name": "Source"},
                                        document=document, kinds=["roms"], sources=self.sources, fetch_page=self.fetch)
        return plan

    def wait_for(self, state, key="state", section=None):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = service.restore_status()
            value = (status.get(section) or {}).get(key) if section else status.get(key)
            if value == state:
                return status
            time.sleep(0.02)
        self.fail(f"restore never reached {state}: {service.restore_status()}")

    def test_assets_are_queued_only_after_a_successful_restore(self):
        plan = self.make_plan()
        queued_while = []
        with mock.patch.object(asset_restore, "fetch_plan",
                               side_effect=lambda *a, **k: queued_while.append(service.restore_status()["state"]) or
                               {"batch_id": "b", "queued": 1, "skipped": 0, "unavailable": 0, "selected": 1, "job_ids": ["j"], "space": []}):
            runner = mock.Mock(return_value={"written": 1, "removed": 0})
            started = service.start_restore(self.settings, self.row["backup_id"], categories=["batocera"], mode="overlay",
                                            runner=runner, assets={"plan_id": plan["plan_id"]}, manager=self.manager)
            self.assertEqual(started["assets"], {"state": "pending"})
            status = self.wait_for("queued", section="assets")
        self.assertEqual(queued_while, ["complete"])
        self.assertEqual(status["assets"]["batch_id"], "b")
        self.assertNotIn("job_ids", status["assets"])

    def test_a_failed_restore_queues_nothing(self):
        plan = self.make_plan()
        with mock.patch.object(asset_restore, "fetch_plan") as fetch:
            runner = mock.Mock(side_effect=archive.BackupError("disk full"))
            service.start_restore(self.settings, self.row["backup_id"], categories=["batocera"], mode="overlay",
                                  runner=runner, assets={"plan_id": plan["plan_id"]}, manager=self.manager)
            status = self.wait_for("skipped", section="assets")
            fetch.assert_not_called()
        self.assertEqual(status["state"], "error")
        self.assertIn("no games or media were queued", status["assets"]["error"])

    def test_a_stale_plan_or_missing_queue_is_refused_before_anything_changes(self):
        with self.assertRaises(archive.BackupError) as raised:
            service.start_restore(self.settings, self.row["backup_id"], categories=["batocera"], mode="overlay",
                                  runner=mock.Mock(), assets={"plan_id": "0" * 32}, manager=self.manager)
        self.assertEqual(raised.exception.status, 409)
        plan = self.make_plan()
        with self.assertRaises(archive.BackupError) as raised:
            service.start_restore(self.settings, self.row["backup_id"], categories=["batocera"], mode="overlay",
                                  runner=mock.Mock(), assets={"plan_id": plan["plan_id"]}, manager=None)
        self.assertEqual(raised.exception.status, 503)
        self.assertIn(service.restore_status().get("state"), (None, "idle"))

    def test_get_missing_games_works_without_restoring_and_can_be_rerun(self):
        runner = mock.Mock()
        with mock.patch.object(asset_restore, "peer_inventory_fetcher", return_value=self.fetch), \
                mock.patch.object(asset_restore, "online_sources", return_value=self.sources):
            self.rows("p1", "roms", peer_rom("snes", "Zelda.sfc", FP_ZELDA, 10))
            # Re-run twice: nothing is persisted between runs.
            for _ in range(2):
                plan = service.plan_assets(self.settings, self.row["backup_id"], kinds=["roms"], online_ids=["p1"], manager=self.manager)
                self.assertEqual(plan["plan_id"] and plan["totals"]["unavailable"], 0)
        runner.assert_not_called()

    def test_plan_assets_reports_the_identity_hold_as_swarm_unavailable(self):
        from app.transfer.peer_connectivity import PeerIdentityHold
        self.broken = {}

        def held(source, kind, params, timeout):
            raise PeerIdentityHold("verifying")

        assets_doc = self.document(roms=[{"system": "snes", "path": "Zelda.sfc", "size": 1, "fp": FP_ZELDA}])
        with mock.patch.object(service, "load_assets", return_value=assets_doc), \
                mock.patch.object(asset_restore, "online_sources", return_value=self.sources):
            with self.assertRaises(asset_restore.SwarmUnavailable):
                service.plan_assets(self.settings, self.row["backup_id"], kinds=["roms"], online_ids=["p1"],
                                    manager=self.manager, fetch_page=held)

    def test_nothing_queued_reports_unavailable_items_with_a_notification(self):
        recorded = []
        with mock.patch.object(notifications, "record_event", side_effect=lambda *a, **k: recorded.append(a)):
            service._notify_assets(self.settings, self.row, {"queued": 0, "unavailable": 4})
            service._notify_assets(self.settings, self.row, {"queued": 2, "unavailable": 4})
            service._notify_assets(self.settings, self.row, {"queued": 0, "unavailable": 0})
        self.assertEqual(len(recorded), 1)
        self.assertIn("4 item(s) are not available", recorded[0][3])

    def test_fetch_and_restore_reject_409_insufficient_space_without_modifying_state(self):
        plan = self.make_plan()
        with mock.patch("app.backups.asset_restore.shutil.disk_usage", return_value=mock.Mock(free=5, total=1000)), \
                mock.patch.dict("os.environ", {"DRONE_ASSET_RESTORE_RESERVE_BYTES": "100"}):
            with self.assertRaises(archive.BackupError) as raised:
                service.fetch_assets(self.settings, self.manager, self.row["backup_id"], {"plan_id": plan["plan_id"]})
            self.assertEqual(raised.exception.status, 409)
            self.assertEqual(raised.exception.details.get("error_code"), "insufficient_space")
            runner = mock.Mock()
            with self.assertRaises(archive.BackupError) as raised_restore:
                service.start_restore(self.settings, self.row["backup_id"], categories=["batocera"], mode="overlay",
                                      runner=runner, assets={"plan_id": plan["plan_id"]}, manager=self.manager)
            self.assertEqual(raised_restore.exception.status, 409)
            self.assertEqual(raised_restore.exception.details.get("error_code"), "insufficient_space")
            runner.assert_not_called()


class HandlerTests(PlanFixture):
    def setUp(self):
        super().setUp()
        from app.web.handlers_backups import HandlersBackupsMixin
        outer = self
        self.row = service.create_backup(self.settings, name="Source", categories=[], wait=True) if False else None

        class Handler(HandlersBackupsMixin):
            settings = outer.settings
            json = None

            def _send_json(self, status, body, **kwargs):
                self.json = (status, body)

            def _media_online_peer_ids(self):
                return {"p1"}

        self.handler = Handler()

    def test_routes_are_wired_and_gated(self):
        row = service.create_backup(self.settings, name="Source", categories=["batocera"], wait=True) \
            if (self.userdata / "system/batocera.conf").exists() else None
        self.write("system/batocera.conf", "a=b\n")
        row = service.create_backup(self.settings, name="Source", categories=["batocera"], wait=True)
        with mock.patch("app.web.handlers_backups._get_download_manager", return_value=None), \
                mock.patch.object(asset_restore, "peer_inventory_fetcher", return_value=self.fetch), \
                mock.patch.object(asset_restore, "online_sources", return_value=self.sources):
            self.handler._handle_admin_backups_post([row["backup_id"], "assets", "plan"], {"kinds": ["roms"]})
            status, body = self.handler.json
            self.assertEqual(status, 200)
            self.assertIn("plan_id", body)
            self.handler._handle_admin_backups_post([row["backup_id"], "assets", "fetch"], {"plan_id": body["plan_id"]})
            self.assertEqual(self.handler.json[0], 400)  # nothing selected
            self.handler._handle_admin_backups_post([row["backup_id"], "assets", "fetch"], {"plan_id": "x" * 32})
            self.assertEqual(self.handler.json[0], 409)
            self.handler._handle_admin_backups_post([row["backup_id"], "assets", "plan"], {"kinds": ["saves"]})
            self.assertEqual(self.handler.json[0], 400)
        # Mutations need the same-origin guard as every other backup route.
        self.handler._multiplayer_origin_ok = lambda: False
        self.handler._handle_admin_backups_post([row["backup_id"], "assets", "plan"], {})
        self.assertEqual(self.handler.json[0], 403)

    def test_download_batch_cancel_route(self):
        from app.web.handlers_downloads import HandlersDownloadsMixin
        manager = mock.Mock()
        manager.cancel_batch.return_value = {"status": "cancelled", "batch_id": "b", "cancelled": 2}

        class Handler(HandlersDownloadsMixin):
            def _send_json(self, status, body, **kwargs):
                self.json = (status, body)

        handler = Handler()
        with mock.patch("app.web.handlers_downloads._get_download_manager", return_value=manager):
            handler._handle_admin_download_batch_cancel("b")
        self.assertEqual(handler.json[0], 200)
        manager.cancel_batch.assert_called_once_with("b", "cancelled from Drone admin")
        manager.cancel_batch.return_value = {"status": "not_found"}
        with mock.patch("app.web.handlers_downloads._get_download_manager", return_value=manager):
            handler._handle_admin_download_batch_cancel("b")
        self.assertEqual(handler.json[0], 404)


class InstallDownloadTests(Fixture):
    def test_a_downloaded_backup_carries_its_asset_record(self):
        self.write("system/batocera.conf", "a=b\n")
        self.write("music/A/b.mp3", "t")
        music_store.sync_music_cache(self.settings.music_root)
        row = service.create_backup(self.settings, name="Source", categories=["batocera"], wait=True)
        original = service.archive_path(self.settings, row["backup_id"])
        sha = archive.file_sha256(original)
        # A second Drone installs the verified archive.
        other = Fixture()
        other.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(other.tmp.cleanup)
        root = Path(other.tmp.name).resolve() / "userdata"
        root.mkdir()
        settings = make_settings(root)
        partial = root / "incoming.part"
        partial.write_bytes(original.read_bytes())
        service.install_download(settings, partial, backup_id=row["backup_id"], expected_sha256=sha, peer={"drone_id": "p1"})
        document = service.load_assets(settings, row["backup_id"])
        self.assertEqual([item["path"] for item in document["music"]], ["A/b.mp3"])
        self.assertTrue(service.assets_path(settings, row["backup_id"]).is_file())
        # Without the sidecar the list is read back out of the archive.
        service.assets_path(settings, row["backup_id"]).unlink()
        self.assertEqual(service.load_assets(settings, row["backup_id"])["music"], document["music"])
        service.delete(settings, row["backup_id"])
        self.assertFalse(service.assets_path(settings, row["backup_id"]).exists())


class BrowserUiAssetRestoreTests(unittest.TestCase):
    def test_backups_js_and_media_browse_js_asset_restore_and_theme_invariants(self):
        root = Path(__file__).resolve().parents[1]
        backups_js = (root / "app/web/static/js/backups.js").read_text(encoding="utf-8")
        media_js = (root / "app/web/static/js/media_browse.js").read_text(encoding="utf-8")

        # Updated help text explains that ROMs/BIOS are not stored in the archive,
        # but are recorded and can be fetched from the swarm.
        self.assertIn("not stored in the archive, but every backup records which ones this Drone had", backups_js)
        self.assertNotIn("ROMs, BIOS files, firmware and caches are not backed up.", backups_js)

        # Required entry points and routes (via api/apiPost without /v1/api prefix).
        self.assertIn("bkOpenMissingGames(", backups_js)
        self.assertIn("Also fetch games &amp; media from the swarm", backups_js)
        self.assertIn("/assets/plan", backups_js)
        self.assertIn("/assets/fetch", backups_js)
        self.assertIn("/admin/downloads/batches/", backups_js)
        self.assertIn("Game list only (older backup)", backups_js)
        self.assertNotIn("/v1/api/admin/backups", backups_js)

        # Theme contract: every table/modal/accordion uses themed-* classes.
        self.assertIn("accordion themed-accordion", backups_js)
        self.assertIn("themed-table", backups_js)
        self.assertIn("themed-modal", backups_js)
        self.assertIn("btn-close-white", backups_js)

        # Transfer toast supports tracking and cancelling a whole batch.
        self.assertIn("trackBatch(batchId, label)", media_js)
        self.assertIn("cancelBatch()", media_js)
        self.assertIn("/admin/downloads/batches/", media_js)

    def test_backups_js_plan_rendering_escapes_peer_data_and_defaults_probable_unticked(self):
        import shutil
        import subprocess
        node = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
        if not node:
            self.skipTest("node is required for JS execution test")
        script = r"""
const fs = require("fs");
const assert = require("assert");
const code = fs.readFileSync("app/web/static/js/backups.js", "utf8");
global.escapeHtml = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
global.jsAttr = (s) => JSON.stringify(String(s ?? "")).replace(/"/g, "&quot;");
global.formatBytes = (n) => `${Number(n || 0)} B`;
global._apiRequestUrl = (u) => `/v1/api${u}`;
global.CSS = { escape: (s) => String(s) };
global.window = { location: { hash: "#admin/backups" } };
global.document = { getElementById: () => null, querySelectorAll: () => [] };
const fn = new Function(code + `; return {
  bkAssetsSummaryLine,
  bkInitAssetPlanState,
  bkRenderAssetPlanHtml,
  bkComputeAssetSelectionStats,
  bkBuildAssetSelection,
  bkToggleProbableAssetItem,
  bkToggleAssetGroup,
  bkUntickUntilFits,
  BK,
};`)();

assert.strictEqual(
  fn.bkAssetsSummaryLine({ recorded: true, counts: { roms: 12, bios: 3, movies: 1, music: 4 }, media_roms: 10 }),
  "Records 12 games (10 with artwork), 3 BIOS, 1 movie, 4 music tracks"
);
assert.strictEqual(fn.bkAssetsSummaryLine({ recorded: false, legacy: true }), "Game list only (older backup)");

const state = fn.bkInitAssetPlanState("missing", "b1", "My Backup", {
  recorded: true,
  counts: { roms: 2, bios: 1, movies: 0, music: 1 },
});
// BIOS is off by default; recorded roms and music are on; movies (0) is off.
assert.strictEqual(state.kinds.has("roms"), true);
assert.strictEqual(state.kinds.has("music"), true);
assert.strictEqual(state.kinds.has("movies"), false);
assert.strictEqual(state.kinds.has("bios"), false);

state.plan = {
  plan_id: "p1",
  totals: { here: 1, available: 1, probable: 1, unavailable: 1 },
  groups: [{
    key: "roms:snes",
    kind: "roms",
    system: "snes",
    count: 1,
    bytes: 200,
    items: [{
      key: "r0",
      kind: "roms",
      system: "snes",
      path: "Zelda.sfc",
      name: "<img src=x onerror=evil()>",
      size: 200,
      action: "download",
      artwork: ["image"],
      peers: [{ drone_id: "p1", name: "<script>bad</script>" }],
    }],
  }],
  probable: [{
    key: "r1",
    kind: "roms",
    system: "snes",
    path: "Metroid.sfc",
    name: "Metroid",
    size: 300,
    action: "download",
    artwork: [],
    peers: [{ drone_id: "p1", name: "Peer 1" }],
  }],
  unavailable: [{
    key: "r2",
    kind: "roms",
    system: "snes",
    path: "Kirby.sfc",
    name: "Kirby",
    size: 100,
    action: "",
  }],
  sources: [{ drone_id: "p1", name: "<b>Peer</b>", status: "ok", matched: 2 }],
  space: { roms: { path: "/userdata/roms", free: 250, group: "g1" } },
};
fn.BK.missing = state;
const html = fn.bkRenderAssetPlanHtml(state);
assert(!html.includes("<img src=x onerror=evil()>"), "peer item name must be escaped");
assert(!html.includes("<script>bad</script>"), "peer name must be escaped");
assert(html.includes("&lt;img src=x onerror=evil()&gt;"));
assert(html.includes("accordion themed-accordion"));
assert(html.includes("themed-table"));

// Exact match is selected by default (200 B <= 250 B free); probable match is unticked.
let stats = fn.bkComputeAssetSelectionStats(state);
assert.strictEqual(stats.selectedCount, 1);
assert.strictEqual(stats.selectedBytes, 200);
assert.strictEqual(stats.pools[0].short, false);

// Tick the probable match -> 500 B > 250 B free -> space check fails and Untick until it fits resolves it.
fn.bkToggleProbableAssetItem("missing", "r1", true);
stats = fn.bkComputeAssetSelectionStats(state);
assert.strictEqual(stats.selectedCount, 2);
assert.strictEqual(stats.selectedBytes, 500);
assert.strictEqual(stats.pools[0].short, true);
assert.strictEqual(stats.pools[0].fits, false);
assert.strictEqual(stats.fits, false);
fn.bkUntickUntilFits("missing");
stats = fn.bkComputeAssetSelectionStats(state);
assert.strictEqual(stats.fits, true);
assert.strictEqual(stats.selectedCount, 1);
fn.bkToggleProbableAssetItem("missing", "r1", true);
const sel = fn.bkBuildAssetSelection(state);
assert.deepStrictEqual(sel.selected, ["r1"]);
"""
        result = subprocess.run([node, "-e", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
