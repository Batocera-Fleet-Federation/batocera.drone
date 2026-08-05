"""Network Share admin feature: mount a paired peer's Batocera Samba ROM
share and reconcile it into roms_root as symlinks (renaming any locally
colliding system folder aside to ``<system>.old`` first, never deleting it).
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
    return settings


_PEER = {"drone_id": "peer-1", "name": "batocera", "tailnet_ip": "100.121.183.109"}


def _mount_that_populates(mount_point: Path, systems: dict):
    """Return a fake subprocess.run side effect that, on the `mount` call,
    actually creates the mount_point dir with the given {system: [files]}
    layout -- standing in for a real CIFS mount for test purposes."""

    def _run(args, **kwargs):
        if args[0] == "mount":
            mount_point.mkdir(parents=True, exist_ok=True)
            for system, files in systems.items():
                system_dir = mount_point / system
                system_dir.mkdir(parents=True, exist_ok=True)
                for name in files:
                    (system_dir / name).write_text("data")
            return mock.Mock(returncode=0, stdout="", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")

    return _run


class ShouldIncludeSystemTests(unittest.TestCase):
    def test_excludes_old_suffixed_names(self) -> None:
        self.assertFalse(network_share_manager._should_include_system("snes.old"))
        self.assertFalse(network_share_manager._should_include_system("SNES.OLD"))

    def test_includes_ordinary_names(self) -> None:
        self.assertTrue(network_share_manager._should_include_system("snes"))


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


class EnableTests(unittest.TestCase):
    def _settings_with_peer(self, tmp: str) -> Settings:
        settings = _build_settings(self, Path(tmp))
        self._get_paired_peer_patcher = mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=_PEER)
        self.addCleanup(self._get_paired_peer_patcher.stop)
        self._get_paired_peer_patcher.start()
        return settings

    def test_mount_failure_never_touches_local_roms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            (settings.roms_root / "snes").mkdir()
            (settings.roms_root / "snes" / "game.zip").write_text("real local rom")
            failed = mock.Mock(returncode=1, stdout="", stderr="mount error(13): Permission denied\n")
            with mock.patch.object(network_share_manager.subprocess, "run", return_value=failed):
                record = network_share_manager.enable(settings, "peer-1")
            self.assertEqual(record["status"], "error")
            self.assertTrue((settings.roms_root / "snes" / "game.zip").is_file())
            self.assertFalse((settings.roms_root / "snes.old").exists())

    def test_no_local_collision_creates_plain_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, {"snes": ["mario.zip"]})):
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
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, {"snes": ["peer_game.zip"]})):
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
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, {"snes": ["peer_game.zip"]})):
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
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, {"snes": ["mario.zip"], "genesis.old": ["hidden.zip"]})):
                record = network_share_manager.enable(settings, "peer-1")
            names = [row["system"] for row in record["systems"]]
            self.assertEqual(names, ["snes"])
            self.assertFalse((settings.roms_root / "genesis.old").exists())
            self.assertFalse((settings.roms_root / "genesis").exists())

    def test_reenabling_is_idempotent_and_leaves_correct_symlink_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            run = _mount_that_populates(mount_point, {"snes": ["mario.zip"]})
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=run):
                network_share_manager.enable(settings, "peer-1")
                second = network_share_manager.enable(settings, "peer-1")
            self.assertEqual(second["status"], "mounted")
            [system_row] = second["systems"]
            self.assertTrue(system_row["symlink_created"])


class DisableTests(unittest.TestCase):
    def _enabled_share(self, tmp: str, *, with_collision: bool):
        settings = _build_settings(self, Path(tmp))
        with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=_PEER):
            if with_collision:
                local_snes = settings.roms_root / "snes"
                local_snes.mkdir()
                (local_snes / "local_game.zip").write_text("real local rom")
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, {"snes": ["peer_game.zip"]})):
                network_share_manager.enable(settings, "peer-1")
        return settings

    def test_disable_restores_renamed_folder_and_removes_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._enabled_share(tmp, with_collision=True)
            with mock.patch.object(network_share_manager.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                result = network_share_manager.disable(settings, "peer-1")
            self.assertEqual(result["status"], "disabled")
            local_snes = settings.roms_root / "snes"
            self.assertFalse(local_snes.is_symlink())
            self.assertTrue((local_snes / "local_game.zip").is_file())
            self.assertFalse((settings.roms_root / "snes.old").exists())

    def test_disable_without_collision_just_removes_the_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._enabled_share(tmp, with_collision=False)
            with mock.patch.object(network_share_manager.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                network_share_manager.disable(settings, "peer-1")
            self.assertFalse((settings.roms_root / "snes").exists())

    def test_disable_removes_the_stored_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._enabled_share(tmp, with_collision=False)
            with mock.patch.object(network_share_manager.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                network_share_manager.disable(settings, "peer-1")
            self.assertIsNone(network_share_manager.get_share(settings, "peer-1"))

    def test_disable_of_unknown_peer_is_a_safe_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            result = network_share_manager.disable(settings, "never-enabled")
            self.assertEqual(result["status"], "not_found")


class StatusAndBootReplayTests(unittest.TestCase):
    def test_status_flips_to_peer_unreachable_when_mount_point_not_actually_mounted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=_PEER):
                mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
                with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, {"snes": ["mario.zip"]})):
                    network_share_manager.enable(settings, "peer-1")
            # os.path.ismount() is false for a plain directory (no real mount syscall happened in this test).
            [share] = network_share_manager.status(settings)
            self.assertEqual(share["status"], "peer_unreachable")

    def test_boot_replay_never_raises_on_a_broken_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=_PEER):
                mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
                with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, {"snes": ["mario.zip"]})):
                    network_share_manager.enable(settings, "peer-1")
            with mock.patch.object(network_share_manager, "resolve_peer_target", side_effect=RuntimeError("boom")):
                network_share_manager.maybe_reconnect_all_on_boot(settings)  # must not raise

    def test_watchdog_remounts_a_dead_share_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=_PEER):
                mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
                with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, {"snes": ["mario.zip"]})):
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
