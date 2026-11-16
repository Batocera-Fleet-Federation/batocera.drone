"""Categorized backups: collection, secret removal, archive verification, restore, swarm."""
import hashlib
import io
import json
import os
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import apply_backup
from app.backups import archive, categories, confmerge, restore, service
from app.storage import backup_store

BACKUP_ID = "0123456789abcdef0123456789abcdef"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.userdata = self.base / "userdata"
        self.settings = SimpleNamespace(userdata_root=self.userdata, roms_root=self.userdata / "roms",
                                        saves_root=self.userdata / "saves", device_id="drone-local",
                                        use_fake_data=False, http_only=False, admin_enabled=True)
        self.roots = categories.Roots.from_settings(self.settings)
        self.write("system/batocera.conf", "wifi.ssid=home\nwifi.key=SECRET\nglobal.shaderset=crt\nglobal.videomode=CEA 4\n")
        self.write("system/configs/retroarch/retroarchcustom.cfg", 'video_driver = "gl"\ncheevos_password = "pw"\n')
        self.write("system/configs/retroarch/config/remaps/snes9x/Mario.rmp", "input_player1_btn_a = 1\n")
        self.write("system/configs/emulationstation/es_input.cfg", "<inputList/>")
        self.write("system/configs/emulationstation/es_settings.cfg", "<config/>")
        self.write("system/configs/dolphin-emu/Dolphin.ini", "[General]\nISOPath0 = /userdata/roms/gc\n[Achievements]\nToken = abc\n")
        self.write("system/configs/dolphin-emu/shader_cache/x.ini", "cache")
        self.write("system/configs/yuzu/nand/system/firmware.nca", "firmware")
        self.write("saves/snes/Mario.srm", "save")
        self.write("saves/snes/Mario.state1", "state")
        self.write("saves/snes/Mario.state1.png", "thumb")
        self.write("saves/psvita/vs0/sys.bin", "firmware")
        self.write("roms/snes/gamelist.xml", "<gameList/>")
        self.write("cheats/snes/Mario.cht", "cheat")
        self.addCleanup(service._RESTORE.clear)
        self.addCleanup(service._ACTIVE_BUILDS.clear)

    def write(self, rel, text):
        path = self.userdata / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode() if isinstance(text, str) else text)
        return path

    def read(self, rel):
        return (self.userdata / rel).read_text()

    def build(self, cats=None, backup_id=BACKUP_ID, dest=None):
        dest = dest or self.base / f"{backup_id}.tar.gz"
        manifest = archive.build_archive(self.roots, dest, backup_id=backup_id, kind="backup", name="Known good",
                                         description="", categories=cats, source={"drone_id": "drone-local"})
        return dest, manifest


class CategoryTests(Fixture):
    def test_collects_each_file_into_its_restorable_category(self):
        files = {item.arcpath: item.category for item in categories.collect(self.roots).files}
        self.assertEqual(files["system/batocera.conf"], "batocera")
        self.assertEqual(files["system/configs/retroarch/retroarchcustom.cfg"], "retroarch")
        self.assertEqual(files["system/configs/retroarch/config/remaps/snes9x/Mario.rmp"], "controllers")
        self.assertEqual(files["system/configs/emulationstation/es_input.cfg"], "controllers")
        self.assertEqual(files["system/configs/emulationstation/es_settings.cfg"], "emulationstation")
        self.assertEqual(files["system/configs/dolphin-emu/Dolphin.ini"], "emulator:dolphin-emu")
        self.assertEqual(files["saves/snes/Mario.srm"], "saves:snes")
        self.assertEqual(files["saves/snes/Mario.state1"], "states:snes")
        self.assertEqual(files["saves/snes/Mario.state1.png"], "states:snes")
        self.assertEqual(files["roms/snes/gamelist.xml"], "gamelists")
        self.assertEqual(files["cheats/snes/Mario.cht"], "cheats")

    def test_excludes_caches_firmware_and_links(self):
        os.symlink(self.userdata / "saves/snes/Mario.srm", self.userdata / "saves/snes/link.srm")
        collection = categories.collect(self.roots)
        paths = {item.arcpath for item in collection.files}
        self.assertNotIn("system/configs/dolphin-emu/shader_cache/x.ini", paths)
        self.assertNotIn("system/configs/yuzu/nand/system/firmware.nca", paths)
        self.assertNotIn("saves/psvita/vs0/sys.bin", paths)
        self.assertNotIn("saves/snes/link.srm", paths)
        reasons = {item["path"]: item["reason"] for item in collection.skipped}
        self.assertEqual(reasons["saves/snes/link.srm"], "symbolic link")

    def test_selected_categories_limit_the_walk(self):
        files = categories.collect(self.roots, ["states:snes"]).files
        self.assertEqual({item.arcpath for item in files}, {"saves/snes/Mario.state1", "saves/snes/Mario.state1.png"})

    def test_classify_rejects_paths_outside_known_roots(self):
        self.assertIsNone(categories.classify("system/drone-app/credentials.json"))
        self.assertIsNone(categories.classify("bios/scph1001.bin"))
        self.assertIsNone(categories.classify("system/configs/dolphin-emu/logo.png"))


class ConfMergeTests(unittest.TestCase):
    def test_scrub_removes_secret_keys_with_their_section(self):
        text, redacted = confmerge.scrub("[General]\nA = 1\n[Achievements]\nToken = abc\nUser = me\n", ".ini")
        self.assertNotIn("abc", text)
        self.assertIn("User = me", text)
        self.assertEqual(redacted, [["Achievements", "Token"]])

    def test_secret_detection(self):
        for key in ("wifi.key", "wifi2.key", "global.retroachievements.password", "cheevos_token",
                    "scraper.screenscraper.password", "global.netplay.password", "api_key"):
            self.assertTrue(confmerge.is_secret_key(key), key)
        for key in ("global.shaderset", "input_player1_a", "wifi.ssid", "hotkey"):
            self.assertFalse(confmerge.is_secret_key(key), key)

    def test_merge_keeps_local_secrets_and_hardware_keys(self):
        merged, changes = confmerge.merge_batocera_conf(
            "wifi.ssid=home\nglobal.shaderset=crt\nglobal.videomode=CEA 4\n",
            "wifi.ssid=office\nwifi.key=NEW\nglobal.shaderset=none\nglobal.videomode=CEA 16\nlocal.only=1\n",
            exact=False, include_machine_keys=False)
        values = confmerge.parse_values(merged)
        self.assertEqual(values["wifi.ssid"], "office")
        self.assertEqual(values["wifi.key"], "NEW")
        self.assertEqual(values["global.videomode"], "CEA 16")
        self.assertEqual(values["global.shaderset"], "crt")
        self.assertEqual(values["local.only"], "1")
        self.assertEqual([row["key"] for row in changes["changed"]], ["global.shaderset"])
        self.assertFalse(any(row["key"] == "wifi.key" for rows in changes.values() for row in rows))

    def test_exact_merge_drops_local_only_keys_and_machine_keys_can_be_opted_in(self):
        merged, changes = confmerge.merge_batocera_conf(
            "global.videomode=CEA 4\n", "global.videomode=CEA 16\nlocal.only=1\nwifi.key=K\n",
            exact=True, include_machine_keys=True)
        values = confmerge.parse_values(merged)
        self.assertEqual(values, {"global.videomode": "CEA 4", "wifi.key": "K"})
        self.assertEqual([row["key"] for row in changes["removed"]], ["local.only"])

    def test_restore_redacted_puts_local_values_back_in_their_section(self):
        text = confmerge.restore_redacted("[General]\nA = 2\n[Achievements]\nUser = me\n",
                                          "[General]\nA = 1\n[Achievements]\nToken = mine\nUser = me\n",
                                          [["Achievements", "Token"]])
        self.assertEqual(text, "[General]\nA = 2\n[Achievements]\nUser = me\nToken = mine\n")


class ArchiveTests(Fixture):
    def test_archive_never_contains_secrets_and_verifies(self):
        dest, manifest = self.build()
        with tarfile.open(dest, "r:gz") as tar:
            blob = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())
        self.assertNotIn(b"SECRET", blob)
        self.assertNotIn(b'cheevos_password = "pw"', blob)
        self.assertNotIn(b"Token = abc", blob)
        verified = archive.verify_archive(dest, expected_id=BACKUP_ID, expected_sha256=archive.file_sha256(dest))
        self.assertEqual(verified["files"], manifest["files"])

    def _forge(self, members, manifest):
        dest = self.base / "forged.tar.gz"
        with tarfile.open(dest, "w:gz") as tar:
            for name, data, kind in members:
                info = tarfile.TarInfo(name)
                if kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/etc/passwd"
                    tar.addfile(info)
                    continue
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            payload = json.dumps(manifest).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        return dest

    def _manifest(self, files):
        return {"format": archive.FORMAT, "version": 1, "backup_id": BACKUP_ID, "kind": "backup", "files": files}

    def _entry(self, path, data, category):
        return {"path": path, "category": category, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def test_rejects_changed_content(self):
        dest = self._forge([("data/saves/snes/a.srm", b"evil", "file")],
                           self._manifest([self._entry("saves/snes/a.srm", b"good", "saves:snes")]))
        with self.assertRaises(archive.BackupError):
            archive.verify_archive(dest, expected_id=BACKUP_ID)

    def test_rejects_traversal_links_and_unlisted_members(self):
        cases = [
            [("data/saves/../../etc/x", b"x", "file")],
            [("data/saves/snes/a.srm", b"", "symlink")],
            [("data/saves/snes/a.srm", b"good", "file"), ("data/saves/snes/extra.srm", b"x", "file")],
            [("other/file", b"x", "file")],
        ]
        for members in cases:
            dest = self._forge(members, self._manifest([self._entry("saves/snes/a.srm", b"good", "saves:snes")]))
            with self.assertRaises(archive.BackupError, msg=str(members)):
                archive.verify_archive(dest, expected_id=BACKUP_ID)

    def test_rejects_mislabelled_category_and_wrong_id(self):
        data = b"#!/bin/sh\n"
        dest = self._forge([("data/system/custom.sh", data, "file")],
                           self._manifest([self._entry("system/custom.sh", data, "saves:snes")]))
        with self.assertRaises(archive.BackupError):
            archive.verify_archive(dest)
        dest, _ = self.build()
        with self.assertRaises(archive.BackupError):
            archive.verify_archive(dest, expected_id="f" * 32)
        with self.assertRaises(archive.BackupError):
            archive.verify_archive(dest, expected_sha256="0" * 64)


class RestoreTests(Fixture):
    def test_overlay_and_exact_modes(self):
        dest, manifest = self.build()
        self.write("saves/snes/Mario.srm", "newer")
        self.write("saves/snes/Extra.srm", "extra")
        self.write("system/batocera.conf", "wifi.key=NEW\nglobal.shaderset=none\nglobal.videomode=CEA 16\n")
        plan = restore.plan(self.roots, dest, manifest, ["saves:snes", "batocera"], "exact")
        rows = {row["id"]: row for row in plan["categories"]}
        self.assertEqual((rows["saves:snes"]["changed"], rows["saves:snes"]["remove"]), (1, 1))
        self.assertEqual(plan["removals"], ["saves/snes/Extra.srm"])
        overlay = restore.apply(self.roots, dest, manifest, ["saves:snes"], "overlay", False, self.base / "staging")
        self.assertEqual((overlay["written"], overlay["removed"]), (1, 0))
        self.assertTrue((self.userdata / "saves/snes/Extra.srm").exists())
        exact = restore.apply(self.roots, dest, manifest, ["saves:snes", "batocera"], "exact", False, self.base / "staging")
        self.assertEqual(exact["removed"], 1)
        self.assertFalse((self.userdata / "saves/snes/Extra.srm").exists())
        self.assertEqual(self.read("saves/snes/Mario.srm"), "save")
        self.assertTrue((self.userdata / "saves/snes/Mario.state1").exists(), "unselected states category untouched")
        values = confmerge.parse_values(self.read("system/batocera.conf"))
        self.assertEqual(values["wifi.key"], "NEW")
        self.assertEqual(values["global.videomode"], "CEA 16")
        self.assertEqual(values["global.shaderset"], "crt")
        self.assertEqual(list((self.base / "staging").iterdir()), [])

    def test_never_writes_through_a_symlinked_directory(self):
        dest, manifest = self.build(["saves:snes"])
        outside = self.base / "outside"
        outside.mkdir()
        import shutil
        shutil.rmtree(self.userdata / "saves/snes")
        os.symlink(outside, self.userdata / "saves/snes")
        result = restore.apply(self.roots, dest, manifest, ["saves:snes"], "overlay", False, self.base / "staging")
        self.assertEqual(result["written"], 0)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(list(outside.iterdir()), [])

    def test_never_replaces_a_symlink_target(self):
        dest, manifest = self.build(["saves:snes"])
        victim = self.base / "victim"
        victim.write_text("keep")
        (self.userdata / "saves/snes/Mario.srm").unlink()
        os.symlink(victim, self.userdata / "saves/snes/Mario.srm")
        result = restore.apply(self.roots, dest, manifest, ["saves:snes"], "overlay", False, self.base / "staging")
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(victim.read_text(), "keep")

    def test_rejects_categories_the_backup_does_not_have(self):
        dest, manifest = self.build(["saves:snes"])
        with self.assertRaises(restore.RestoreError):
            restore.plan(self.roots, dest, manifest, ["scripts"], "overlay")
        with self.assertRaises(restore.RestoreError):
            restore.plan(self.roots, dest, manifest, ["saves:snes"], "wipe")

    def test_redacted_secrets_keep_local_values_on_restore(self):
        dest, manifest = self.build(["retroarch"])
        self.write("system/configs/retroarch/retroarchcustom.cfg", 'video_driver = "vulkan"\ncheevos_password = "mine"\n')
        restore.apply(self.roots, dest, manifest, ["retroarch"], "overlay", False, self.base / "staging")
        self.assertEqual(self.read("system/configs/retroarch/retroarchcustom.cfg"),
                         'video_driver = "gl"\ncheevos_password = "mine"\n')


class ServiceTests(Fixture):
    def _run(self, settings, request):
        return apply_backup.run(request, manage_emulationstation=False)

    def _wait_restore(self):
        for _ in range(200):
            status = service.restore_status()
            if status.get("state") != "running":
                return status
            time.sleep(0.05)
        self.fail("restore did not finish")

    def test_create_restore_snapshot_and_roll_back(self):
        row = service.create_backup(self.settings, name="Known good", categories=["saves:snes", "states:snes"], wait=True)
        self.assertEqual(row["status"], "complete")
        backup_id = row["backup_id"]
        self.write("saves/snes/Mario.srm", "broken")
        self.write("saves/snes/New.srm", "added later")
        plan = service.plan(self.settings, backup_id, categories=["saves:snes"], mode="exact")
        self.assertEqual(plan["totals"]["remove"], 1)
        service.start_restore(self.settings, backup_id, categories=["saves:snes"], mode="exact", runner=self._run)
        status = self._wait_restore()
        self.assertEqual(status["state"], "complete", status)
        self.assertEqual(self.read("saves/snes/Mario.srm"), "save")
        self.assertFalse((self.userdata / "saves/snes/New.srm").exists())
        listing = service.list_local(self.settings)
        self.assertEqual(len(listing["snapshots"]), 1)
        snapshot = listing["snapshots"][0]
        self.assertEqual(snapshot["snapshot_of"]["categories"], ["saves:snes"])
        service.start_restore(self.settings, snapshot["backup_id"], categories=["saves:snes"], mode="exact", runner=self._run)
        self.assertEqual(self._wait_restore()["state"], "complete")
        self.assertEqual(self.read("saves/snes/Mario.srm"), "broken")
        self.assertEqual(self.read("saves/snes/New.srm"), "added later")

    def test_roll_back_of_a_previously_empty_category_removes_added_files(self):
        self.write("saves/gba/Zelda.srm", "remote save")
        row = service.create_backup(self.settings, categories=["saves:gba"], wait=True)
        (self.userdata / "saves/gba/Zelda.srm").unlink()
        service.start_restore(self.settings, row["backup_id"], categories=["saves:gba"], mode="overlay", runner=self._run)
        self._wait_restore()
        snapshot = service.list_local(self.settings)["snapshots"][0]
        service.start_restore(self.settings, snapshot["backup_id"], categories=["saves:gba"], mode="exact", runner=self._run)
        self.assertEqual(self._wait_restore()["state"], "complete")
        self.assertFalse((self.userdata / "saves/gba/Zelda.srm").exists())

    def test_compatibility_warnings_need_acknowledgement(self):
        row = service.create_backup(self.settings, categories=["batocera"], wait=True)
        backup_store.mark_complete(self.settings, row["backup_id"], dict(
            service.local_manifest(self.settings, row["backup_id"]),
            source={"drone_id": "other", "batocera_version": "39", "architecture": "zz-arch"}),
            size_bytes=row["size_bytes"], archive_sha256=row["archive_sha256"])
        with self.assertRaises(service.BackupError) as raised:
            service.start_restore(self.settings, row["backup_id"], categories=["batocera"], mode="overlay", runner=self._run)
        self.assertEqual(raised.exception.status, 409)
        self.assertTrue(raised.exception.details["warnings"])
        service.start_restore(self.settings, row["backup_id"], categories=["batocera"], mode="overlay",
                              acknowledge_warnings=True, runner=self._run)
        self.assertEqual(self._wait_restore()["state"], "complete")

    def test_snapshot_failure_aborts_restore(self):
        row = service.create_backup(self.settings, categories=["saves:snes"], wait=True)
        runner = mock.Mock()
        with mock.patch.object(archive, "build_archive", side_effect=OSError("disk full")):
            service.start_restore(self.settings, row["backup_id"], categories=["saves:snes"], mode="overlay", runner=runner)
            status = self._wait_restore()
        self.assertEqual(status["state"], "error")
        runner.assert_not_called()

    def test_snapshots_are_pruned_and_never_shared(self):
        row = service.create_backup(self.settings, categories=["cheats"], wait=True)
        for _ in range(service.SNAPSHOT_KEEP + 2):
            service.start_restore(self.settings, row["backup_id"], categories=["cheats"], mode="overlay", runner=self._run)
            self._wait_restore()
        listing = service.list_local(self.settings)
        self.assertEqual(len(listing["snapshots"]), service.SNAPSHOT_KEEP)
        shared = service.peer_listing(self.settings)["backups"]
        self.assertEqual([item["backup_id"] for item in shared], [row["backup_id"]])
        self.assertNotIn("files", shared[0])
        with self.assertRaises(service.BackupError):
            service.peer_archive(self.settings, listing["snapshots"][0]["backup_id"])

    def test_only_one_user_backup_builds_at_a_time(self):
        service._ACTIVE_BUILDS["f" * 32] = "backup"
        with self.assertRaises(service.BackupError) as raised:
            service.create_backup(self.settings)
        self.assertEqual(raised.exception.status, 409)

    def test_interrupted_builds_are_marked_failed(self):
        backup_store.create_pending(self.settings, "e" * 32, kind="backup", name="x", description="")
        listing = service.list_local(self.settings)
        self.assertEqual(listing["backups"][0]["status"], "error")

    def test_install_download_verifies_and_registers_peer_backup(self):
        dest, manifest = self.build(backup_id="c" * 32, dest=self.base / "peer.tar.gz")
        sha = archive.file_sha256(dest)
        row = service.install_download(self.settings, dest, backup_id="c" * 32, expected_sha256=sha,
                                       peer={"drone_id": "peer-1", "name": "Den"})
        self.assertEqual(row["origin"], "peer")
        self.assertEqual(row["downloaded_from_name"], "Den")
        self.assertTrue(service.archive_path(self.settings, "c" * 32).is_file())
        dest2, _ = self.build(backup_id="d" * 32, dest=self.base / "peer2.tar.gz")
        with self.assertRaises(service.BackupError):
            service.install_download(self.settings, dest2, backup_id="d" * 32, expected_sha256="0" * 64,
                                     peer={"drone_id": "peer-1"})
        self.assertFalse(dest2.exists())
        self.assertIsNone(backup_store.get(self.settings, "d" * 32))

    def test_peer_listing_entries_are_sanitized_and_queued_through_download_manager(self):
        listing = {"backups": [
            {"backup_id": "c" * 32, "name": "<b>Den</b>", "size_bytes": 10, "archive_sha256": "a" * 64,
             "categories": [{"id": "saves:snes", "file_count": 2}, {"id": "../evil"}], "source": {"batocera_version": "42"}},
            {"backup_id": "../../x", "size_bytes": 1, "archive_sha256": "a" * 64},
        ]}
        with mock.patch.object(service, "_peer_json", return_value=({"name": "Den"}, listing)):
            entries = service.peer_backups(self.settings, "peer-1")
            self.assertEqual(len(entries), 1)
            self.assertEqual([c["id"] for c in entries[0]["categories"]], ["saves:snes"])
            manager = mock.Mock()
            manager.enqueue_backup.return_value = {"id": "job-1"}
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": "peer-1"}):
                result = service.enqueue_download(self.settings, manager, "peer-1", "c" * 32)
        self.assertEqual(result["status"], "queued")
        kwargs = manager.enqueue_backup.call_args.kwargs
        self.assertEqual((kwargs["expected_size"], kwargs["expected_sha256"]), (10, "a" * 64))


class HandlerTests(Fixture):
    def _handler(self, authorized=True):
        from app.web.handlers_backups import HandlersBackupsMixin

        class Handler(HandlersBackupsMixin):
            pass

        handler = Handler()
        handler.settings = self.settings
        handler.sent = []
        handler.headers = {}
        handler._send_json = lambda status, body: handler.sent.append((status, body))
        handler._peer_request_authorized = lambda: authorized or handler.sent.append((403, {})) and False
        handler._stream_file = mock.Mock()
        return handler

    def test_peer_routes_require_authorization_and_https(self):
        handler = self._handler(authorized=False)
        handler._handle_peer_backups([])
        self.assertEqual(handler.sent[0][0], 403)
        self.settings.http_only = True
        handler = self._handler()
        handler._handle_peer_backups([])
        self.assertEqual(handler.sent[0][0], 403)

    def test_restore_conflict_reports_warnings(self):
        row = service.create_backup(self.settings, categories=["cheats"], wait=True)
        handler = self._handler()
        with mock.patch.object(service, "_warnings", return_value=["Different Batocera"]):
            handler._handle_admin_backups_post([row["backup_id"], "restore"], {"categories": ["cheats"], "mode": "overlay"})
        status, body = handler.sent[0]
        self.assertEqual(status, 409)
        self.assertEqual(body["warnings"], ["Different Batocera"])

    def test_cross_origin_mutations_are_refused(self):
        handler = self._handler()
        handler._multiplayer_origin_ok = lambda: False
        handler._handle_admin_backups_post([], {})
        self.assertEqual(handler.sent[0][0], 403)

    def test_unknown_backup_is_404(self):
        handler = self._handler()
        handler._handle_admin_backups_get(["not-an-id"], {})
        self.assertEqual(handler.sent[0][0], 404)


if __name__ == "__main__":
    unittest.main()
