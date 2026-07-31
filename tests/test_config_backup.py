import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app.device.config_backup as config_backup
import app.storage.config_backup_store as config_backup_store
from app.common.settings import Settings


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "local-test",
        "LOG_DIR": str(root / "logs"),
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def _write(path: Path, content: bytes = b"data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _wait_for_status(settings, backup_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = config_backup_store.get(settings, backup_id)
        if row and row["status"] != config_backup_store.STATUS_CREATING:
            return row
        time.sleep(0.02)
    raise AssertionError(f"backup {backup_id} still creating after {timeout}s")


class ConfigBackupStoreTests(unittest.TestCase):
    def test_create_pending_then_list_and_get(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = config_backup_store.create_pending(settings, "drone-config-backup-test.tar.gz")
            self.assertEqual(row["status"], config_backup_store.STATUS_CREATING)
            self.assertIsInstance(row["id"], int)

            fetched = config_backup_store.get(settings, row["id"])
            self.assertEqual(fetched["file_name"], "drone-config-backup-test.tar.gz")

            listed = config_backup_store.list_all(settings)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["id"], row["id"])

    def test_mark_complete_and_mark_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = config_backup_store.create_pending(settings, "a.tar.gz")
            config_backup_store.mark_complete(
                settings, row["id"], size_bytes=123, included_file_count=4, skipped_file_count=1, skipped_bytes=99
            )
            fetched = config_backup_store.get(settings, row["id"])
            self.assertEqual(fetched["status"], config_backup_store.STATUS_COMPLETE)
            self.assertEqual(fetched["size_bytes"], 123)
            self.assertEqual(fetched["skipped_file_count"], 1)

            row2 = config_backup_store.create_pending(settings, "b.tar.gz")
            config_backup_store.mark_error(settings, row2["id"], "disk full")
            fetched2 = config_backup_store.get(settings, row2["id"])
            self.assertEqual(fetched2["status"], config_backup_store.STATUS_ERROR)
            self.assertEqual(fetched2["error_message"], "disk full")

    def test_delete_removes_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = config_backup_store.create_pending(settings, "a.tar.gz")
            self.assertTrue(config_backup_store.delete(settings, row["id"]))
            self.assertIsNone(config_backup_store.get(settings, row["id"]))
            self.assertFalse(config_backup_store.delete(settings, row["id"]))

    def test_create_pending_carries_name_and_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = config_backup_store.create_pending(settings, "a.tar.gz", name="Weekly", description="Just in case")
            self.assertEqual(row["name"], "Weekly")
            self.assertEqual(row["description"], "Just in case")
            self.assertTrue(row["is_local"])
            fetched = config_backup_store.get(settings, row["id"])
            self.assertEqual(fetched["name"], "Weekly")
            self.assertEqual(fetched["description"], "Just in case")

    def test_create_pending_defaults_name_and_description_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = config_backup_store.create_pending(settings, "a.tar.gz")
            self.assertEqual(row["name"], "")
            self.assertEqual(row["description"], "")

    def test_get_by_file_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = config_backup_store.create_pending(settings, "a.tar.gz")
            fetched = config_backup_store.get_by_file_name(settings, "a.tar.gz")
            self.assertEqual(fetched["id"], row["id"])
            self.assertIsNone(config_backup_store.get_by_file_name(settings, "missing.tar.gz"))

    def test_record_downloaded_creates_complete_row_with_source_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = config_backup_store.record_downloaded(
                settings,
                file_name="peer-a.tar.gz",
                size_bytes=555,
                name="Pulled backup",
                description="from peer",
                source_drone_id="drone-b",
                source_drone_name="Living Room",
                source_created_at="2026-01-01T00:00:00+00:00",
            )
            self.assertEqual(row["status"], config_backup_store.STATUS_COMPLETE)
            self.assertEqual(row["size_bytes"], 555)
            self.assertEqual(row["source_drone_id"], "drone-b")
            self.assertEqual(row["source_drone_name"], "Living Room")
            self.assertFalse(row["is_local"])
            # any_creating() must never see a downloaded backup as in-progress.
            self.assertFalse(config_backup_store.any_creating(settings))

    def test_list_complete_page_filters_status_and_searches_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            complete = config_backup_store.create_pending(settings, "done.tar.gz", name="Weekly Snapshot")
            config_backup_store.mark_complete(
                settings, complete["id"], size_bytes=10, included_file_count=1, skipped_file_count=0, skipped_bytes=0
            )
            config_backup_store.create_pending(settings, "building.tar.gz", name="In progress")

            page = config_backup_store.list_complete_page(settings)
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["items"][0]["file_name"], "done.tar.gz")

            match = config_backup_store.list_complete_page(settings, query="weekly")
            self.assertEqual(match["total"], 1)
            no_match = config_backup_store.list_complete_page(settings, query="nonexistent")
            self.assertEqual(no_match["total"], 0)

    def test_any_creating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertFalse(config_backup_store.any_creating(settings))
            row = config_backup_store.create_pending(settings, "a.tar.gz")
            self.assertTrue(config_backup_store.any_creating(settings))
            config_backup_store.mark_complete(
                settings, row["id"], size_bytes=1, included_file_count=1, skipped_file_count=0, skipped_bytes=0
            )
            self.assertFalse(config_backup_store.any_creating(settings))


class ConfigBackupSourceSelectionTests(unittest.TestCase):
    def test_includes_batocera_conf_and_gamelist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            _write(root / "system" / "batocera.conf", b"conf")
            _write(root / "roms" / "snes" / "gamelist.xml", b"<gameList/>")
            _write(root / "roms" / "snes" / "game.zip", b"not included")

            included, skipped = config_backup.collect_sources(settings)
            arcnames = {arc for _path, arc, _size in included}
            self.assertIn("system/batocera.conf", arcnames)
            self.assertIn("roms/snes/gamelist.xml", arcnames)
            self.assertNotIn("roms/snes/game.zip", arcnames)
            self.assertEqual(skipped, [])

    def test_skips_large_config_files_but_keeps_small_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            _write(root / "system" / "configs" / "retroarch" / "retroarch.cfg", b"small")
            big_content = b"0" * (config_backup.BACKUP_MAX_CONFIG_FILE_BYTES + 1)
            _write(root / "system" / "configs" / "citron" / "huge.xml", big_content)

            included, skipped = config_backup.collect_sources(settings)
            arcnames = {arc for _path, arc, _size in included}
            self.assertIn("system/configs/retroarch/retroarch.cfg", arcnames)
            self.assertNotIn("system/configs/citron/huge.xml", arcnames)
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0]["path"], "system/configs/citron/huge.xml")
            self.assertIn("20MB limit", skipped[0]["reason"])

    def test_excludes_images_audio_and_firmware_under_configs_regardless_of_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            _write(root / "system" / "configs" / "retroarch" / "retroarch.cfg", b"real setting")
            _write(root / "system" / "configs" / "hypseus-singe" / "custom.ini", b"real setting")
            _write(root / "system" / "configs" / "hypseus-singe" / "pics" / "spaceace.bmp", b"tiny-but-not-config")
            _write(root / "system" / "configs" / "dolphin5-triforce" / "dolphin" / "Sys" / "Resources" / "Dolphin.png", b"icon")
            _write(root / "system" / "configs" / "hypseus-singe" / "sound" / "theme.wav", b"audio")
            _write(root / "system" / "configs" / "citron" / "game.nca", b"firmware-ish")
            _write(root / "system" / "configs" / "Ryujinx" / "system" / "nand_blob", b"no-extension-firmware")

            included, skipped = config_backup.collect_sources(settings)
            arcnames = {arc for _path, arc, _size in included}
            self.assertIn("system/configs/retroarch/retroarch.cfg", arcnames)
            self.assertIn("system/configs/hypseus-singe/custom.ini", arcnames)
            self.assertNotIn("system/configs/hypseus-singe/pics/spaceace.bmp", arcnames)
            self.assertNotIn("system/configs/dolphin5-triforce/dolphin/Sys/Resources/Dolphin.png", arcnames)
            self.assertNotIn("system/configs/hypseus-singe/sound/theme.wav", arcnames)
            self.assertNotIn("system/configs/citron/game.nca", arcnames)
            self.assertNotIn("system/configs/Ryujinx/system/nand_blob", arcnames)
            reasons = {entry["path"]: entry["reason"] for entry in skipped}
            self.assertIn("not a recognized configuration file type", reasons["system/configs/hypseus-singe/pics/spaceace.bmp"])

    def test_excludes_shader_cache_directories_under_configs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            _write(root / "system" / "configs" / "mesa_shader_cache" / "index" / "abc123", b"cache")
            _write(root / "system" / "configs" / "retroarch" / "retroarch.cfg", b"real setting")

            included, skipped = config_backup.collect_sources(settings)
            arcnames = {arc for _path, arc, _size in included}
            self.assertIn("system/configs/retroarch/retroarch.cfg", arcnames)
            self.assertNotIn("system/configs/mesa_shader_cache/index/abc123", arcnames)
            reasons = {entry["path"]: entry["reason"] for entry in skipped}
            self.assertIn("cache directory", reasons["system/configs/mesa_shader_cache/index/abc123"])

    def test_excludes_emulator_firmware_and_disk_images_under_saves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            _write(root / "saves" / "snes" / "game.srm", b"real save")
            _write(root / "saves" / "psvita" / "vs0" / "data" / "internal" / "keylock.png", b"vita firmware")
            _write(root / "saves" / "psvita" / "ux0" / "user" / "00" / "savedata" / "save0", b"real vita save")
            _write(root / "saves" / "yuzu" / "0000000000000000" / "nand_blob", b"switch firmware")
            _write(root / "saves" / "xbox" / "xbox_hdd.qcow2", b"virtual disk")
            _write(root / "saves" / "mesa_shader_cache" / "index" / "deadbeef", b"cache")

            included, skipped = config_backup.collect_sources(settings)
            arcnames = {arc for _path, arc, _size in included}
            self.assertIn("saves/snes/game.srm", arcnames)
            self.assertIn("saves/psvita/ux0/user/00/savedata/save0", arcnames)
            self.assertNotIn("saves/psvita/vs0/data/internal/keylock.png", arcnames)
            self.assertNotIn("saves/yuzu/0000000000000000/nand_blob", arcnames)
            self.assertNotIn("saves/xbox/xbox_hdd.qcow2", arcnames)
            self.assertNotIn("saves/mesa_shader_cache/index/deadbeef", arcnames)
            reasons = {entry["path"]: entry["reason"] for entry in skipped}
            self.assertIn("firmware", reasons["saves/psvita/vs0/data/internal/keylock.png"])
            self.assertIn("firmware", reasons["saves/yuzu/0000000000000000/nand_blob"])
            self.assertIn("disk-image", reasons["saves/xbox/xbox_hdd.qcow2"])
            self.assertIn("cache", reasons["saves/mesa_shader_cache/index/deadbeef"])

    def test_includes_saves_and_services_and_custom_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            _write(root / "saves" / "snes" / "game.srm", b"save")
            _write(root / "system" / "services" / "DRONE_SERVER", b"#!/bin/sh")
            _write(root / "system" / "custom-scripts" / "border.sh", b"#!/bin/sh")
            _write(root / "system" / "custom.sh", b"#!/bin/sh")
            _write(root / "system" / "pro-custom.sh", b"#!/bin/sh")

            included, _skipped = config_backup.collect_sources(settings)
            arcnames = {arc for _path, arc, _size in included}
            self.assertIn("saves/snes/game.srm", arcnames)
            self.assertIn("system/services/DRONE_SERVER", arcnames)
            self.assertIn("system/custom-scripts/border.sh", arcnames)
            self.assertIn("system/custom.sh", arcnames)
            self.assertIn("system/pro-custom.sh", arcnames)

    def test_excludes_bios_and_rom_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            _write(root / "bios" / "scph5501.bin", b"bios")
            _write(root / "roms" / "psx" / "game.chd", b"rom")

            included, _skipped = config_backup.collect_sources(settings)
            arcnames = {arc for _path, arc, _size in included}
            self.assertEqual(arcnames, set())


class ConfigBackupBuildTests(unittest.TestCase):
    def test_create_backup_builds_downloadable_tarball(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            _write(root / "system" / "batocera.conf", b"conf-bytes")
            _write(root / "saves" / "snes" / "game.srm", b"save-bytes")

            result = config_backup.create_backup(settings)
            self.assertEqual(result["status"], "ok")
            backup_id = result["backup"]["id"]

            row = _wait_for_status(settings, backup_id)
            self.assertEqual(row["status"], config_backup_store.STATUS_COMPLETE)
            self.assertEqual(row["included_file_count"], 2)
            self.assertGreater(row["size_bytes"], 0)

            tarball_path = config_backup.backups_directory(settings) / row["file_name"]
            self.assertTrue(tarball_path.is_file())
            with tarfile.open(tarball_path, "r:gz") as tar:
                names = set(tar.getnames())
            self.assertIn("MANIFEST.txt", names)
            self.assertIn("system/batocera.conf", names)
            self.assertIn("saves/snes/game.srm", names)

    def test_create_backup_rejects_concurrent_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            # Simulate an in-flight build without racing a real background
            # thread: any_creating() is the sole source of truth, so a
            # "creating" row is all that's needed to trigger the guard.
            config_backup_store.create_pending(settings, "already-running.tar.gz")
            result = config_backup.create_backup(settings)
            self.assertEqual(result["status"], "already_creating")

    def test_delete_backup_removes_file_and_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            _write(root / "system" / "batocera.conf", b"conf-bytes")

            result = config_backup.create_backup(settings)
            row = _wait_for_status(settings, result["backup"]["id"])
            tarball_path = config_backup.backups_directory(settings) / row["file_name"]
            self.assertTrue(tarball_path.is_file())

            delete_result = config_backup.delete_backup(settings, row["id"])
            self.assertEqual(delete_result["status"], "deleted")
            self.assertFalse(tarball_path.is_file())
            self.assertIsNone(config_backup_store.get(settings, row["id"]))

    def test_delete_backup_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = config_backup.delete_backup(settings, 999)
            self.assertEqual(result["status"], "not_found")

    def test_build_backup_tree_lists_files_with_sizes_no_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            _write(root / "system" / "batocera.conf", b"conf-bytes")
            _write(root / "system" / "configs" / "retroarch" / "retroarch.cfg", b"cfg-bytes-longer")
            _write(root / "saves" / "snes" / "game.srm", b"save-bytes")

            result = config_backup.create_backup(settings)
            row = _wait_for_status(settings, result["backup"]["id"])

            tree = config_backup.build_backup_tree(settings, row["id"])
            self.assertEqual(tree["status"], "ok")
            self.assertEqual(tree["file_name"], row["file_name"])
            by_path = {entry["relative_path"]: entry["size"] for entry in tree["files"]}
            self.assertEqual(by_path["system/batocera.conf"], len(b"conf-bytes"))
            self.assertEqual(by_path["system/configs/retroarch/retroarch.cfg"], len(b"cfg-bytes-longer"))
            self.assertEqual(by_path["saves/snes/game.srm"], len(b"save-bytes"))
            self.assertIn("MANIFEST.txt", by_path)
            # Read-only: no file contents anywhere in the response.
            self.assertNotIn("conf-bytes", str(tree))

    def test_build_backup_tree_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertEqual(config_backup.build_backup_tree(settings, 999)["status"], "not_found")

    def test_build_backup_tree_rejects_incomplete_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = config_backup_store.create_pending(settings, "still-building.tar.gz")
            self.assertEqual(config_backup.build_backup_tree(settings, row["id"])["status"], "not_found")

    def test_build_failure_marks_error_not_stuck_creating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            _write(root / "system" / "batocera.conf", b"conf-bytes")

            with mock.patch.object(config_backup, "_build_tarball", side_effect=OSError("disk full")):
                result = config_backup.create_backup(settings)
            row = _wait_for_status(settings, result["backup"]["id"])
            self.assertEqual(row["status"], config_backup_store.STATUS_ERROR)
            self.assertIn("disk full", row["error_message"])


class ApplyBackupToMachineTests(unittest.TestCase):
    def _completed_backup(self, root: Path, settings: Settings) -> dict:
        _write(root / "system" / "batocera.conf", b"conf-bytes")
        result = config_backup.create_backup(settings)
        return _wait_for_status(settings, result["backup"]["id"])

    def test_not_found_when_backup_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertEqual(config_backup.apply_backup_to_machine(settings, 999)["status"], "not_found")

    def test_not_found_when_backup_still_creating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            row = config_backup_store.create_pending(settings, "still-building.tar.gz")
            self.assertEqual(config_backup.apply_backup_to_machine(settings, row["id"])["status"], "not_found")

    def test_root_direct_calls_restore_helper_under_es_lifecycle_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            row = self._completed_backup(root, settings)
            with mock.patch("app.device.config_backup.os.geteuid", return_value=0), \
                 mock.patch(
                     "app.device.config_backup._restore_config_backup_helper",
                     return_value={"restored_file_count": 1, "skipped_file_count": 0, "restarted_emulationstation": True},
                 ) as helper:
                result = config_backup.apply_backup_to_machine(settings, row["id"])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["restored_file_count"], 1)
            self.assertTrue(result["restarted_emulationstation"])
            helper.assert_called_once_with(
                config_backup.backups_directory(settings) / row["file_name"],
                settings.userdata_root, settings.roms_root, settings.saves_root,
            )

    def test_root_direct_records_config_backup_applied_notification(self) -> None:
        from app.storage import audit_store

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            row = self._completed_backup(root, settings)
            with mock.patch("app.device.config_backup.os.geteuid", return_value=0), \
                 mock.patch(
                     "app.device.config_backup._restore_config_backup_helper",
                     return_value={"restored_file_count": 1, "skipped_file_count": 0, "restarted_emulationstation": True},
                 ):
                config_backup.apply_backup_to_machine(settings, row["id"])
            events = audit_store.list_unsent_events(settings, event_types=["config_backup_applied"])
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["title"], "Config backup applied to this machine")

    def test_root_direct_helper_failure_returns_error_status_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            row = self._completed_backup(root, settings)
            with mock.patch("app.device.config_backup.os.geteuid", return_value=0), \
                 mock.patch(
                     "app.device.config_backup._restore_config_backup_helper",
                     side_effect=RuntimeError("EmulationStation did not restart"),
                 ):
                result = config_backup.apply_backup_to_machine(settings, row["id"])
            self.assertEqual(result["status"], "error")
            self.assertIn("did not restart", result["error"])

    def test_non_root_dispatches_to_privileged_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            row = self._completed_backup(root, settings)
            with mock.patch("app.device.config_backup.os.geteuid", return_value=999), \
                 mock.patch(
                     "app.device.config_backup._request_config_backup_apply_service_control", return_value=True
                 ) as dispatch:
                result = config_backup.apply_backup_to_machine(settings, row["id"])
            self.assertEqual(result["status"], "ok")
            dispatch.assert_called_once()
            request_payload = dispatch.call_args.args[0]
            self.assertEqual(request_payload["archive_path"], str(config_backup.backups_directory(settings) / row["file_name"]))
            self.assertEqual(request_payload["userdata_root"], str(settings.userdata_root))

    def test_non_root_worker_unavailable_returns_error_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            row = self._completed_backup(root, settings)
            with mock.patch("app.device.config_backup.os.geteuid", return_value=999), \
                 mock.patch(
                     "app.device.config_backup._request_config_backup_apply_service_control", return_value=False
                 ):
                result = config_backup.apply_backup_to_machine(settings, row["id"])
            self.assertEqual(result["status"], "error")

    def test_fake_data_short_circuits_without_dispatching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "local-test",
                "LOG_DIR": str(root / "logs"),
                "USE_FAKE_DATA": "true",
            }
            with mock.patch.dict("os.environ", env, clear=True):
                settings = Settings.from_env()
            row = self._completed_backup(root, settings)
            with mock.patch("app.device.config_backup._request_config_backup_apply_service_control") as dispatch, \
                 mock.patch("app.device.config_backup._restore_config_backup_helper") as helper:
                result = config_backup.apply_backup_to_machine(settings, row["id"])
            self.assertEqual(result["status"], "ok")
            dispatch.assert_not_called()
            helper.assert_not_called()


class ConfigBackupTreeUiContentTests(unittest.TestCase):
    """The extension-count summary is computed client-side from the tree
    endpoint's existing file list (no backend change) -- these just confirm
    it's actually wired into the modal, mirroring the codebase's existing
    grep-based content tests for other frontend-only additions."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.js = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")

    def _function_body(self, name: str) -> str:
        start = self.js.index(name)
        brace_start = self.js.index("{", start)
        depth = 0
        i = brace_start
        while i < len(self.js):
            char = self.js[i]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return self.js[start:i + 1]
            i += 1
        raise AssertionError(f"unbalanced braces scanning function starting {name!r}")

    def test_extension_summary_function_defined_and_used_in_modal(self) -> None:
        self.assertIn("function summarizeConfigBackupExtensions(files)", self.js)
        self.assertIn("function renderConfigBackupExtensionSummary(files)", self.js)
        modal_body = self._function_body("async function openConfigBackupTreeModal(")
        self.assertIn("renderConfigBackupExtensionSummary(files)", modal_body)

    def test_extension_summary_buckets_no_extension_files_together(self) -> None:
        body = self._function_body("function configBackupFileExtension(relativePath)")
        self.assertIn('"(no extension)"', body)


if __name__ == "__main__":
    unittest.main()
