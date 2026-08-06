"""Network Share admin feature: mount a paired peer's whole Batocera Samba
share (roms/ and bios/ both under it) and reconcile each into roms_root/
bios_root as symlinks -- renaming any locally colliding system folder or BIOS
file aside with an ``.old`` suffix first, never deleting it.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.network_share_manager as network_share_manager
from app.drone_api import Settings


def _build_settings(test_case: unittest.TestCase, root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "network-share-test",
    }
    patcher = mock.patch.object(network_share_manager, "_drone_install_root", return_value=root / "install-root")
    test_case.addCleanup(patcher.stop)
    patcher.start()
    with mock.patch.dict("os.environ", env, clear=True):
        settings = Settings.from_env()
    settings.roms_root.mkdir(parents=True, exist_ok=True)
    settings.bios_root.mkdir(parents=True, exist_ok=True)
    return settings


_PEER = {"drone_id": "peer-1", "name": "batocera", "tailnet_ip": "100.121.183.109"}


def _mount_that_populates(mount_point: Path, roms: dict = None, bios: dict = None):
    """Return a fake subprocess.run side effect that, on the `mount` call,
    actually creates the mount_point dir with the given layout -- standing in
    for a real CIFS mount of the whole share (roms/ and bios/ both under it)
    for test purposes.

    ``roms`` is ``{system: [filenames]}``. ``bios`` is ``{relative_path:
    "content"}`` -- relative_path may include one subdirectory
    (e.g. "dc/dc_boot.bin") to exercise the per-emulator-subfolder case.
    """

    def _run(args, **kwargs):
        if args[0] == "mount":
            mount_point.mkdir(parents=True, exist_ok=True)
            for system, files in (roms or {}).items():
                system_dir = mount_point / "roms" / system
                system_dir.mkdir(parents=True, exist_ok=True)
                for name in files:
                    (system_dir / name).write_text("data")
            for relative_path, content in (bios or {}).items():
                file_path = mount_point / "bios" / relative_path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content)
            return mock.Mock(returncode=0, stdout="", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")

    return _run


class ShouldIncludeEntryTests(unittest.TestCase):
    def test_excludes_old_suffixed_names(self) -> None:
        self.assertFalse(network_share_manager._should_include_entry("snes.old"))
        self.assertFalse(network_share_manager._should_include_entry("SNES.OLD"))
        self.assertFalse(network_share_manager._should_include_entry("scph5501.bin.old"))

    def test_includes_ordinary_names(self) -> None:
        self.assertTrue(network_share_manager._should_include_entry("snes"))
        self.assertTrue(network_share_manager._should_include_entry("scph5501.bin"))


class ResolvePeerTargetTests(unittest.TestCase):
    def test_resolves_from_paired_peer_record_not_client_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=_PEER):
                target = network_share_manager.resolve_peer_target(settings, "peer-1")
            self.assertEqual(target["tailnet_ip"], "100.121.183.109")
            self.assertEqual(target["peer_name"], "batocera")

    def test_raises_when_not_a_paired_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=None):
                with self.assertRaises(ValueError):
                    network_share_manager.resolve_peer_target(settings, "peer-1")

    def test_raises_when_peer_has_no_tailnet_ip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            peer = {**_PEER, "tailnet_ip": ""}
            with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=peer):
                with self.assertRaises(ValueError):
                    network_share_manager.resolve_peer_target(settings, "peer-1")


class MountTargetTests(unittest.TestCase):
    def test_mounts_the_whole_share_not_just_roms(self) -> None:
        # roms/ and bios/ are both subfolders of one share -- must mount the
        # share itself, not //host/share/roms, so both are reachable.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(network_share_manager.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
                network_share_manager._mount("100.121.183.109", Path(tmp) / "mnt")
            args = run.call_args[0][0]
            self.assertIn("//100.121.183.109/share", args)
            self.assertNotIn("//100.121.183.109/share/roms", args)


class EnableTests(unittest.TestCase):
    def _settings_with_peer(self, tmp: str) -> Settings:
        settings = _build_settings(self, Path(tmp))
        self._get_paired_peer_patcher = mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=_PEER)
        self.addCleanup(self._get_paired_peer_patcher.stop)
        self._get_paired_peer_patcher.start()
        return settings

    def test_mount_failure_never_touches_local_roms_or_bios(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            (settings.roms_root / "snes").mkdir()
            (settings.roms_root / "snes" / "game.zip").write_text("real local rom")
            (settings.bios_root / "scph5501.bin").write_text("real local bios")
            failed = mock.Mock(returncode=1, stdout="", stderr="mount error(13): Permission denied\n")
            with mock.patch.object(network_share_manager.subprocess, "run", return_value=failed):
                record = network_share_manager.enable(settings, "peer-1")
            self.assertEqual(record["status"], "error")
            self.assertTrue((settings.roms_root / "snes" / "game.zip").is_file())
            self.assertFalse((settings.roms_root / "snes.old").exists())
            self.assertTrue((settings.bios_root / "scph5501.bin").is_file())
            self.assertFalse((settings.bios_root / "scph5501.bin.old").exists())

    def test_no_local_collision_creates_plain_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, roms={"snes": ["mario.zip"]})):
                record = network_share_manager.enable(settings, "peer-1")
            self.assertEqual(record["status"], "mounted")
            local_snes = settings.roms_root / "snes"
            self.assertTrue(local_snes.is_symlink())
            self.assertTrue((local_snes / "mario.zip").is_file())
            [system_row] = record["systems"]
            self.assertEqual(system_row["system"], "snes")
            self.assertFalse(system_row["had_local_collision"])
            self.assertEqual(system_row["renamed_to"], "")

    def test_local_collision_renames_to_old_suffix_and_symlinks_over_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            local_snes = settings.roms_root / "snes"
            local_snes.mkdir()
            (local_snes / "local_game.zip").write_text("real local rom")
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, roms={"snes": ["peer_game.zip"]})):
                record = network_share_manager.enable(settings, "peer-1")
            self.assertEqual(record["status"], "mounted")
            # Nothing was deleted -- the original local folder survives, just renamed.
            old_dir = settings.roms_root / "snes.old"
            self.assertTrue(old_dir.is_dir())
            self.assertTrue((old_dir / "local_game.zip").is_file())
            # The "snes" slot now points at the network mount.
            self.assertTrue(local_snes.is_symlink())
            self.assertTrue((local_snes / "peer_game.zip").is_file())
            [system_row] = record["systems"]
            self.assertTrue(system_row["had_local_collision"])
            self.assertEqual(system_row["renamed_to"], "snes.old")

    def test_never_overwrites_a_pre_existing_old_folder_skips_instead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            local_snes = settings.roms_root / "snes"
            local_snes.mkdir()
            (local_snes / "local_game.zip").write_text("real local rom")
            old_dir = settings.roms_root / "snes.old"
            old_dir.mkdir()
            (old_dir / "someone_elses_file.zip").write_text("pre-existing .old content")
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, roms={"snes": ["peer_game.zip"]})):
                record = network_share_manager.enable(settings, "peer-1")
            self.assertEqual(record["status"], "mounted")
            # Untouched: still a real local dir, not a symlink; .old content unchanged.
            self.assertFalse(local_snes.is_symlink())
            self.assertTrue((local_snes / "local_game.zip").is_file())
            self.assertTrue((old_dir / "someone_elses_file.zip").is_file())
            [system_row] = record["systems"]
            self.assertFalse(system_row["symlink_created"])
            self.assertIn("skipped", system_row["skipped_reason"])

    def test_peer_system_folders_ending_in_old_are_not_referenced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, roms={"snes": ["mario.zip"], "genesis.old": ["hidden.zip"]})):
                record = network_share_manager.enable(settings, "peer-1")
            names = [row["system"] for row in record["systems"]]
            self.assertEqual(names, ["snes"])
            self.assertFalse((settings.roms_root / "genesis.old").exists())
            self.assertFalse((settings.roms_root / "genesis").exists())

    def test_reenabling_is_idempotent_and_leaves_correct_symlink_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            run = _mount_that_populates(mount_point, roms={"snes": ["mario.zip"]})
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=run):
                network_share_manager.enable(settings, "peer-1")
                second = network_share_manager.enable(settings, "peer-1")
            self.assertEqual(second["status"], "mounted")
            [system_row] = second["systems"]
            self.assertTrue(system_row["symlink_created"])


class BiosEnableTests(unittest.TestCase):
    def _settings_with_peer(self, tmp: str) -> Settings:
        settings = _build_settings(self, Path(tmp))
        self._get_paired_peer_patcher = mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=_PEER)
        self.addCleanup(self._get_paired_peer_patcher.stop)
        self._get_paired_peer_patcher.start()
        return settings

    def test_no_local_collision_creates_plain_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, bios={"scph5501.bin": "peer bios"})):
                record = network_share_manager.enable(settings, "peer-1")
            self.assertEqual(record["status"], "mounted")
            local_bios = settings.bios_root / "scph5501.bin"
            self.assertTrue(local_bios.is_symlink())
            self.assertEqual(local_bios.read_text(), "peer bios")
            [bios_row] = record["bios"]
            self.assertEqual(bios_row["relative_path"], "scph5501.bin")
            self.assertFalse(bios_row["had_local_collision"])

    def test_nested_per_emulator_subfolder_is_mirrored_as_a_real_directory(self) -> None:
        # Regression: symlinking a whole subfolder (like ROM systems) would be
        # invisible to RomAssetBiosMixin.list_bios_entries()'s os.walk (which
        # doesn't follow symlinked directories by default) -- must mirror the
        # subfolder as a real local directory and symlink the file inside it.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, bios={"dc/dc_boot.bin": "dc bios"})):
                record = network_share_manager.enable(settings, "peer-1")
            local_dir = settings.bios_root / "dc"
            self.assertTrue(local_dir.is_dir())
            self.assertFalse(local_dir.is_symlink())
            local_file = local_dir / "dc_boot.bin"
            self.assertTrue(local_file.is_symlink())
            self.assertEqual(local_file.read_text(), "dc bios")
            [bios_row] = record["bios"]
            self.assertEqual(bios_row["relative_path"], "dc/dc_boot.bin")

    def test_local_collision_renames_file_to_old_suffix_and_symlinks_over_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            local_bios = settings.bios_root / "scph5501.bin"
            local_bios.write_text("real local bios")
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, bios={"scph5501.bin": "peer bios"})):
                record = network_share_manager.enable(settings, "peer-1")
            old_file = settings.bios_root / "scph5501.bin.old"
            self.assertTrue(old_file.is_file())
            self.assertEqual(old_file.read_text(), "real local bios")
            self.assertTrue(local_bios.is_symlink())
            self.assertEqual(local_bios.read_text(), "peer bios")
            [bios_row] = record["bios"]
            self.assertTrue(bios_row["had_local_collision"])
            self.assertEqual(bios_row["renamed_to"], "scph5501.bin.old")

    def test_never_overwrites_a_pre_existing_old_file_skips_instead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            local_bios = settings.bios_root / "scph5501.bin"
            local_bios.write_text("real local bios")
            (settings.bios_root / "scph5501.bin.old").write_text("pre-existing .old content")
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, bios={"scph5501.bin": "peer bios"})):
                record = network_share_manager.enable(settings, "peer-1")
            self.assertFalse(local_bios.is_symlink())
            self.assertEqual(local_bios.read_text(), "real local bios")
            self.assertEqual((settings.bios_root / "scph5501.bin.old").read_text(), "pre-existing .old content")
            [bios_row] = record["bios"]
            self.assertFalse(bios_row["symlink_created"])
            self.assertIn("skipped", bios_row["skipped_reason"])

    def test_peer_bios_files_ending_in_old_are_not_referenced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, bios={"scph5501.bin": "x", "hidden.bin.old": "y"})):
                record = network_share_manager.enable(settings, "peer-1")
            paths = [row["relative_path"] for row in record["bios"]]
            self.assertEqual(paths, ["scph5501.bin"])
            self.assertFalse((settings.bios_root / "hidden.bin.old").exists())
            self.assertFalse((settings.bios_root / "hidden.bin").exists())

    def test_reenabling_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            run = _mount_that_populates(mount_point, bios={"scph5501.bin": "peer bios"})
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=run):
                network_share_manager.enable(settings, "peer-1")
                second = network_share_manager.enable(settings, "peer-1")
            [bios_row] = second["bios"]
            self.assertTrue(bios_row["symlink_created"])

    def test_roms_and_bios_are_reconciled_together_in_one_enable_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            run = _mount_that_populates(mount_point, roms={"snes": ["mario.zip"]}, bios={"scph5501.bin": "peer bios"})
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=run):
                record = network_share_manager.enable(settings, "peer-1")
            self.assertEqual(len(record["systems"]), 1)
            self.assertEqual(len(record["bios"]), 1)
            self.assertTrue((settings.roms_root / "snes").is_symlink())
            self.assertTrue((settings.bios_root / "scph5501.bin").is_symlink())


class DisableTests(unittest.TestCase):
    def _enabled_share(self, tmp: str, *, with_rom_collision: bool, with_bios_collision: bool = False, with_nested_bios: bool = False):
        settings = _build_settings(self, Path(tmp))
        with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=_PEER):
            if with_rom_collision:
                local_snes = settings.roms_root / "snes"
                local_snes.mkdir()
                (local_snes / "local_game.zip").write_text("real local rom")
            if with_bios_collision:
                (settings.bios_root / "scph5501.bin").write_text("real local bios")
            bios = {"dc/dc_boot.bin": "dc bios"} if with_nested_bios else {"scph5501.bin": "peer bios"}
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, roms={"snes": ["peer_game.zip"]}, bios=bios)):
                network_share_manager.enable(settings, "peer-1")
        return settings

    def test_disable_restores_renamed_folder_and_removes_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._enabled_share(tmp, with_rom_collision=True)
            with mock.patch.object(network_share_manager.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                result = network_share_manager.disable(settings, "peer-1")
            self.assertEqual(result["status"], "disabled")
            local_snes = settings.roms_root / "snes"
            self.assertFalse(local_snes.is_symlink())
            self.assertTrue((local_snes / "local_game.zip").is_file())
            self.assertFalse((settings.roms_root / "snes.old").exists())

    def test_disable_without_collision_just_removes_the_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._enabled_share(tmp, with_rom_collision=False)
            with mock.patch.object(network_share_manager.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                network_share_manager.disable(settings, "peer-1")
            self.assertFalse((settings.roms_root / "snes").exists())

    def test_disable_removes_the_stored_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._enabled_share(tmp, with_rom_collision=False)
            with mock.patch.object(network_share_manager.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                network_share_manager.disable(settings, "peer-1")
            self.assertIsNone(network_share_manager.get_share(settings, "peer-1"))

    def test_disable_of_unknown_peer_is_a_safe_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            result = network_share_manager.disable(settings, "never-enabled")
            self.assertEqual(result["status"], "not_found")

    def test_disable_restores_renamed_bios_file_and_removes_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._enabled_share(tmp, with_rom_collision=False, with_bios_collision=True)
            with mock.patch.object(network_share_manager.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                network_share_manager.disable(settings, "peer-1")
            local_bios = settings.bios_root / "scph5501.bin"
            self.assertFalse(local_bios.is_symlink())
            self.assertEqual(local_bios.read_text(), "real local bios")
            self.assertFalse((settings.bios_root / "scph5501.bin.old").exists())

    def test_disable_without_bios_collision_removes_symlink_and_empty_subfolder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._enabled_share(tmp, with_rom_collision=False, with_nested_bios=True)
            with mock.patch.object(network_share_manager.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                network_share_manager.disable(settings, "peer-1")
            self.assertFalse((settings.bios_root / "dc" / "dc_boot.bin").exists())
            # Best-effort cleanup of the now-empty per-emulator subfolder Drone created.
            self.assertFalse((settings.bios_root / "dc").exists())


class StatusAndBootReplayTests(unittest.TestCase):
    def test_status_flips_to_peer_unreachable_when_mount_point_not_actually_mounted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=_PEER):
                mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
                with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, roms={"snes": ["mario.zip"]})):
                    network_share_manager.enable(settings, "peer-1")
            # os.path.ismount() is false for a plain directory (no real mount syscall happened in this test).
            [share] = network_share_manager.status(settings)
            self.assertEqual(share["status"], "peer_unreachable")

    def test_boot_replay_never_raises_on_a_broken_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=_PEER):
                mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
                with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, roms={"snes": ["mario.zip"]})):
                    network_share_manager.enable(settings, "peer-1")
            with mock.patch.object(network_share_manager, "resolve_peer_target", side_effect=RuntimeError("boom")):
                network_share_manager.maybe_reconnect_all_on_boot(settings)  # must not raise

    def test_watchdog_remounts_a_dead_share_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=_PEER):
                mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
                with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, roms={"snes": ["mario.zip"]})):
                    network_share_manager.enable(settings, "peer-1")
            with mock.patch.object(network_share_manager, "_probe_mount_alive", return_value=False), \
                    mock.patch.object(network_share_manager, "time") as fake_time, \
                    mock.patch.object(network_share_manager, "enable") as fake_enable:
                fake_time.sleep.side_effect = [None, StopIteration]
                with self.assertRaises(StopIteration):
                    network_share_manager.run_watchdog_poller(settings)
            fake_enable.assert_called_with(settings, "peer-1")


if __name__ == "__main__":
    unittest.main()
