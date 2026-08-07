"""Network Share admin feature: mount a paired peer's ROM/BIOS library over
NFSv4 (or SMB fallback) and reconcile each into roms_root/
bios_root as symlinks -- renaming locally colliding ROM system folders aside
while always preserving existing local BIOS files.
"""

import subprocess
import tempfile
import threading
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
    bios_patcher = mock.patch.object(network_share_manager, "_bios_reconciliation_background_enabled", return_value=False)
    test_case.addCleanup(patcher.stop)
    test_case.addCleanup(bios_patcher.stop)
    patcher.start()
    bios_patcher.start()
    with mock.patch.dict("os.environ", env, clear=True):
        settings = Settings.from_env()
    settings.roms_root.mkdir(parents=True, exist_ok=True)
    settings.bios_root.mkdir(parents=True, exist_ok=True)
    return settings


_PEER = {"drone_id": "peer-1", "name": "batocera", "tailnet_ip": "100.121.183.109"}
_CAPABILITY_FOR_CLIENT = {
    "available": True,
    "protocol": "nfs",
    "versions": ["4.2", "4.1", "4"],
    "preferred_version": "4.2",
    "port": 2049,
    "detail": "",
}


def _mount_that_populates(mount_point: Path, roms: dict = None, bios: dict = None):
    """Return a fake subprocess.run side effect that, on the `mount` call,
    actually creates the mount_point dir with the given layout -- standing in
    for a real network mount of the whole share (roms/ and bios/ both under it)
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
        self.assertFalse(network_share_manager._should_include_entry("snes.old.20260805T120000"))

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
            self.assertEqual(target["addresses"], ["100.121.183.109"])

    def test_prefers_trusted_lan_address_and_keeps_tailnet_as_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            peer = {
                **_PEER,
                "source_ip": "192.168.0.206",
                "reachable_url": "https://192.168.0.206",
            }
            with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=peer):
                target = network_share_manager.resolve_peer_target(settings, "peer-1")
            self.assertEqual(target["addresses"], ["192.168.0.206", "100.121.183.109"])

    def test_raises_when_not_a_paired_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=None):
                with self.assertRaises(ValueError):
                    network_share_manager.resolve_peer_target(settings, "peer-1")

    def test_accepts_lan_only_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            peer = {**_PEER, "tailnet_ip": "", "reachable_url": "https://192.168.0.206"}
            with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=peer):
                target = network_share_manager.resolve_peer_target(settings, "peer-1")
            self.assertEqual(target["addresses"], ["192.168.0.206"])

    def test_raises_when_peer_has_no_known_address(self) -> None:
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
            options = args[args.index("-o") + 1]
            self.assertIn("ro", options.split(","))
            self.assertIn("soft", options.split(","))
            self.assertIn("noserverino", options.split(","))
            self.assertIn(f"actimeo={network_share_manager.NETWORK_SHARE_ATTRIBUTE_CACHE_SECONDS}", options.split(","))

    def test_unreachable_lan_candidate_falls_back_to_tailnet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(network_share_manager, "_smb_port_open", side_effect=[False, True]), \
                    mock.patch.object(network_share_manager.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
                result = network_share_manager._mount(["192.168.0.206", "100.121.183.109"], Path(tmp) / "mnt")
            self.assertEqual(result, {"status": "mounted", "address": "100.121.183.109"})
            self.assertIn("//100.121.183.109/share", run.call_args.args[0])

    def test_nfs_mount_is_read_only_bounded_and_uses_negotiated_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = {"versions": ["4.2", "4.1"], "preferred_version": "4.1", "export_path": "/"}
            with mock.patch.object(network_share_manager, "_tcp_port_open", return_value=True), \
                    mock.patch.object(
                        network_share_manager.subprocess,
                        "run",
                        return_value=mock.Mock(returncode=0, stdout="", stderr=""),
                    ) as run:
                result = network_share_manager._mount_nfs("100.121.183.109", Path(tmp) / "mnt", contract)

            self.assertEqual(result["protocol"], "nfs")
            self.assertEqual(result["nfs_version"], "4.1")
            args = run.call_args.args[0]
            self.assertEqual(args[:3], ["mount", "-t", "nfs"])
            self.assertIn("100.121.183.109:/", args)
            options = args[args.index("-o") + 1].split(",")
            self.assertIn("ro", options)
            self.assertIn("soft", options)
            self.assertIn("vers=4.1", options)
            self.assertIn(f"timeo={network_share_manager.NETWORK_SHARE_NFS_TIMEOUT_TENTHS}", options)
            self.assertIn(f"actimeo={network_share_manager.NETWORK_SHARE_ATTRIBUTE_CACHE_SECONDS}", options)


class TransportSelectionTests(unittest.TestCase):
    def test_auto_prefers_a_negotiated_nfs_mount(self) -> None:
        target = {"peer_id": "peer-1", "addresses": ["192.168.0.206"], "peer": {"certificate_path": "/cert"}}
        contract = {**_CAPABILITY_FOR_CLIENT, "export_path": "/"}
        with mock.patch.object(network_share_manager, "_negotiate_nfs", return_value=contract), \
                mock.patch.object(
                    network_share_manager,
                    "_mount_nfs",
                    return_value={"status": "mounted", "address": "192.168.0.206", "protocol": "nfs"},
                ) as mount_nfs, mock.patch.object(network_share_manager, "_mount") as mount_smb:
            result = network_share_manager._mount_preferred_transport(mock.Mock(), target, Path("/mnt/peer"))

        self.assertEqual(result["protocol"], "nfs")
        mount_nfs.assert_called_once()
        mount_smb.assert_not_called()

    def test_auto_falls_back_to_smb_and_keeps_the_reason(self) -> None:
        target = {"peer_id": "peer-1", "addresses": ["192.168.0.206"], "peer": {"certificate_path": "/cert"}}
        with mock.patch.object(
            network_share_manager,
            "_negotiate_nfs",
            return_value={"available": False, "detail": "NFS server unavailable"},
        ), mock.patch.object(
            network_share_manager,
            "_mount",
            return_value={"status": "mounted", "address": "192.168.0.206"},
        ) as mount_smb:
            result = network_share_manager._mount_preferred_transport(mock.Mock(), target, Path("/mnt/peer"))

        self.assertEqual(result["protocol"], "smb")
        self.assertEqual(result["transport_fallback_detail"], "NFS server unavailable")
        mount_smb.assert_called_once()

    def test_nfs_only_mode_never_attempts_smb(self) -> None:
        target = {"peer_id": "peer-1", "addresses": ["192.168.0.206"], "peer": {"certificate_path": "/cert"}}
        with mock.patch.dict("os.environ", {"DRONE_NETWORK_SHARE_PROTOCOL": "nfs"}), \
                mock.patch.object(
                    network_share_manager,
                    "_negotiate_nfs",
                    return_value={"available": False, "detail": "NFS server unavailable"},
                ), mock.patch.object(network_share_manager, "_mount") as mount_smb:
            result = network_share_manager._mount_preferred_transport(mock.Mock(), target, Path("/mnt/peer"))

        self.assertEqual(result, {"status": "error", "detail": "NFS server unavailable", "protocol": "nfs"})
        mount_smb.assert_not_called()


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
            self.assertEqual(record["status"], "peer_unreachable")
            self.assertTrue((settings.roms_root / "snes" / "game.zip").is_file())
            self.assertFalse((settings.roms_root / "snes.old").exists())
            self.assertTrue((settings.bios_root / "scph5501.bin").is_file())
            self.assertFalse((settings.bios_root / "scph5501.bin.old").exists())

    def test_existing_active_smb_mount_is_not_hot_switched_to_nfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            (mount_point / "roms").mkdir(parents=True)
            (mount_point / "bios").mkdir()
            network_share_manager._save_state(settings, {"schema_version": 3, "peers": {}})
            network_share_manager._upsert_peer_record(
                settings,
                "peer-1",
                peer_name="batocera",
                enabled=True,
                status="mounted",
                mount_point=str(mount_point),
                mounted_address="192.168.0.206",
                protocol="smb",
            )
            with mock.patch.object(network_share_manager, "_is_mounted", return_value=True), \
                    mock.patch.object(network_share_manager, "_mount_preferred_transport") as select_transport, \
                    mock.patch.object(network_share_manager, "_fetch_peer_summary", return_value={"systems": []}), \
                    mock.patch.object(network_share_manager, "_fetch_peer_bios_paths", return_value=[]):
                record = network_share_manager.enable(settings, "peer-1")

            select_transport.assert_not_called()
            self.assertEqual(record["status"], "mounted")
            self.assertEqual(record["protocol"], "smb")
            self.assertEqual(record["mounted_address"], "192.168.0.206")

    def test_dead_remount_restores_local_folder_instead_of_leaving_dangling_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            local_snes = settings.roms_root / "snes"
            local_snes.mkdir()
            (local_snes / "local.zip").write_text("local")
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(
                network_share_manager.subprocess,
                "run",
                side_effect=_mount_that_populates(mount_point, roms={"snes": ["peer.zip"]}),
            ):
                network_share_manager.enable(settings, "peer-1")
            failed = mock.Mock(returncode=1, stdout="", stderr="host is down")
            with mock.patch.object(network_share_manager.subprocess, "run", return_value=failed):
                record = network_share_manager.enable(settings, "peer-1")

            self.assertEqual(record["status"], "peer_unreachable")
            self.assertFalse(local_snes.is_symlink())
            self.assertEqual((local_snes / "local.zip").read_text(), "local")
            self.assertFalse((settings.roms_root / "snes.old").exists())
            self.assertEqual(record["systems"], [])

    def test_background_bios_reconciliation_does_not_delay_rom_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            run = _mount_that_populates(
                mount_point,
                roms={"snes": ["peer.zip"]},
                bios={"scph5501.bin": "peer bios"},
            )
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=run), \
                    mock.patch.object(network_share_manager, "_bios_reconciliation_background_enabled", return_value=True), \
                    mock.patch.object(network_share_manager, "_schedule_bios_reconciliation", return_value=True) as schedule:
                record = network_share_manager.enable(settings, "peer-1")

            self.assertEqual(record["status"], "mounted")
            self.assertEqual(record["bios_status"], "pending")
            self.assertTrue((settings.roms_root / "snes").is_symlink())
            self.assertFalse((settings.bios_root / "scph5501.bin").exists())
            schedule.assert_called_once()
            scheduled_settings, scheduled_peer, scheduled_mount, scheduled_target = schedule.call_args.args
            self.assertIs(scheduled_settings, settings)
            self.assertEqual(scheduled_peer, "peer-1")
            self.assertEqual(scheduled_mount, mount_point)
            self.assertEqual(scheduled_target["peer_id"], "peer-1")

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

    def test_second_peer_never_replaces_first_peers_system_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            first_mount = network_share_manager.peer_mount_point(settings, "peer-1")
            second_mount = network_share_manager.peer_mount_point(settings, "peer-2")
            local_link = settings.roms_root / "snes"
            local_link.symlink_to(first_mount / "roms" / "snes", target_is_directory=True)

            [row] = network_share_manager._apply_system_references(
                settings,
                second_mount,
                [],
                ["snes"],
            )

            self.assertFalse(row["symlink_created"])
            self.assertIn("another peer", row["skipped_reason"])
            self.assertTrue(network_share_manager._is_network_reference(local_link, first_mount))

    def test_symlink_failure_rolls_local_system_rename_back_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            local_snes = settings.roms_root / "snes"
            local_snes.mkdir()
            (local_snes / "local.zip").write_text("local")
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")

            with mock.patch.object(Path, "symlink_to", side_effect=OSError("denied")):
                [row] = network_share_manager._apply_system_references(settings, mount_point, [], ["snes"])

            self.assertFalse(row["symlink_created"])
            self.assertEqual(row["renamed_to"], "")
            self.assertEqual((local_snes / "local.zip").read_text(), "local")
            self.assertFalse((settings.roms_root / "snes.old").exists())


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

    def test_local_collision_keeps_local_bios_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            local_bios = settings.bios_root / "scph5501.bin"
            local_bios.write_text("real local bios")
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, bios={"scph5501.bin": "peer bios"})):
                record = network_share_manager.enable(settings, "peer-1")
            self.assertFalse((settings.bios_root / "scph5501.bin.old").exists())
            self.assertFalse(local_bios.is_symlink())
            self.assertEqual(local_bios.read_text(), "real local bios")
            self.assertEqual(record["bios"], [])
            self.assertEqual(record["bios_local_count"], 1)
            self.assertEqual(record["bios_remote_count"], 1)

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
            self.assertEqual(record["bios"], [])
            self.assertEqual(record["bios_local_count"], 1)

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

    def test_api_bios_inventory_avoids_walking_the_smb_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            (mount_point / "bios" / "dc").mkdir(parents=True)
            with mock.patch.object(network_share_manager.os, "walk", side_effect=AssertionError("must not walk SMB")):
                bios, _local_count, remote_count = network_share_manager._apply_bios_references(
                    settings,
                    mount_point,
                    [],
                    ["dc/dc_boot.bin", "../escape.bin", "/absolute.bin"],
                )

            self.assertEqual(remote_count, 1)
            self.assertEqual([row["relative_path"] for row in bios], ["dc/dc_boot.bin"])
            self.assertTrue((settings.bios_root / "dc" / "dc_boot.bin").is_symlink())

    def test_background_bios_worker_commits_ready_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_peer(tmp)
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            peer_bios = mount_point / "bios" / "dc" / "dc_boot.bin"
            peer_bios.parent.mkdir(parents=True)
            peer_bios.write_text("peer bios")
            network_share_manager._save_state(settings, {"schema_version": 2, "peers": {}})
            network_share_manager._upsert_peer_record(
                settings,
                "peer-1",
                enabled=True,
                status="mounted",
                mount_point=str(mount_point),
                bios=[],
                bios_status="pending",
            )

            network_share_manager._run_bios_reconciliation(settings, "peer-1", mount_point, None)

            record = network_share_manager.get_share(settings, "peer-1")
            self.assertEqual(record["bios_status"], "ready")
            self.assertEqual(record["bios_remote_count"], 1)
            self.assertTrue((settings.bios_root / "dc" / "dc_boot.bin").is_symlink())


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

    def test_disabling_nfs_mount_revokes_source_authorization_after_local_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            network_share_manager._save_state(settings, {"schema_version": 3, "peers": {}})
            network_share_manager._upsert_peer_record(
                settings,
                "peer-1",
                enabled=True,
                status="mounted",
                mount_point=str(mount_point),
                protocol="nfs",
                systems=[],
                bios=[],
            )
            target = {"peer_id": "peer-1", "addresses": ["100.121.183.109"], "peer": _PEER}
            with mock.patch.object(network_share_manager, "_restore_local_fallback", return_value=[]), \
                    mock.patch.object(network_share_manager, "_unmount"), \
                    mock.patch.object(network_share_manager, "resolve_peer_target", return_value=target), \
                    mock.patch.object(network_share_manager, "_revoke_nfs_export") as revoke:
                result = network_share_manager.disable(settings, "peer-1")

            self.assertEqual(result["status"], "disabled")
            revoke.assert_called_once_with(settings, target)
            self.assertIsNone(network_share_manager.get_share(settings, "peer-1"))

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

    def test_request_disable_persists_intent_before_background_thread_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._enabled_share(tmp, with_rom_collision=True)

            with mock.patch.object(threading.Thread, "start", return_value=None):
                result = network_share_manager.request_disable(settings, "peer-1")

            record = network_share_manager.get_share(settings, "peer-1")
            self.assertEqual(result["status"], "detaching")
            self.assertIsNotNone(record)
            self.assertFalse(record["enabled"])
            self.assertEqual(record["status"], "detaching")
            with network_share_manager._BACKGROUND_JOBS_LOCK:
                network_share_manager._ACTIVE_DISABLE_JOBS.discard(
                    network_share_manager._operation_key(settings, "peer-1")
                )


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
            self.assertNotIn("systems", share)
            self.assertNotIn("bios", share)
            self.assertNotIn("remote_system_counts", share)
            self.assertEqual(share["system_count"], 1)

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
                    mock.patch.object(network_share_manager, "_unmount") as unmount, \
                    mock.patch.object(network_share_manager, "enable") as fake_enable:
                fake_time.sleep.side_effect = [None, StopIteration]
                fake_enable.return_value = {"status": "mounted", "_refresh_required": False}
                with self.assertRaises(StopIteration):
                    network_share_manager.run_watchdog_poller(settings)
            fake_enable.assert_called_with(settings, "peer-1")
            unmount.assert_called_once()

    def test_watchdog_does_not_remount_after_detach_deleted_stale_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            network_share_manager._save_state(settings, {"schema_version": 2, "peers": {}})
            network_share_manager._upsert_peer_record(
                settings,
                "peer-1",
                peer_name="batocera",
                enabled=True,
                status="mounted",
                mount_point=str(network_share_manager.peer_mount_point(settings, "peer-1")),
            )

            def detach_during_probe(_mount_point):
                network_share_manager._delete_peer_record(settings, "peer-1")
                return False

            with mock.patch.object(network_share_manager, "_probe_mount_alive", side_effect=detach_during_probe), \
                    mock.patch.object(network_share_manager, "time") as fake_time, \
                    mock.patch.object(network_share_manager, "_unmount") as unmount, \
                    mock.patch.object(network_share_manager, "enable") as enable:
                fake_time.sleep.side_effect = [None, StopIteration]
                with self.assertRaises(StopIteration):
                    network_share_manager.run_watchdog_poller(settings)

            unmount.assert_not_called()
            enable.assert_not_called()

    def test_boot_replay_finishes_interrupted_detach_and_refreshes_es(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            local_snes = settings.roms_root / "snes"
            local_snes.mkdir()
            (local_snes / "local.zip").write_text("local")
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            with mock.patch.object(network_share_manager._local_network, "get_paired_peer", return_value=_PEER), \
                    mock.patch.object(network_share_manager.subprocess, "run", side_effect=_mount_that_populates(mount_point, roms={"snes": ["peer.zip"]})):
                network_share_manager.enable(settings, "peer-1")
            network_share_manager._upsert_peer_record(
                settings,
                "peer-1",
                enabled=False,
                status="detaching",
                status_detail="power lost after detach acceptance",
            )

            with mock.patch.object(network_share_manager, "_refresh_emulationstation_after_share_change", return_value=True) as refresh, \
                    mock.patch.object(network_share_manager.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                network_share_manager.maybe_reconnect_all_on_boot(settings)

            self.assertIsNone(network_share_manager.get_share(settings, "peer-1"))
            self.assertFalse(local_snes.is_symlink())
            self.assertEqual((local_snes / "local.zip").read_text(), "local")
            self.assertFalse((settings.roms_root / "snes.old").exists())
            refresh.assert_called_once_with()

    def test_service_restart_does_not_refresh_es_when_mount_remained_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            network_share_manager._save_state(settings, {"schema_version": 2, "peers": {}})
            network_share_manager._upsert_peer_record(
                settings,
                "peer-1",
                peer_name="batocera",
                enabled=True,
                status="mounted",
                mount_point=str(network_share_manager.peer_mount_point(settings, "peer-1")),
            )
            with mock.patch.object(
                network_share_manager,
                "enable",
                return_value={"status": "mounted", "_refresh_required": False},
            ), mock.patch.object(
                network_share_manager,
                "_refresh_emulationstation_after_share_change",
                return_value=True,
            ) as refresh:
                network_share_manager.maybe_reconnect_all_on_boot(settings)

            refresh.assert_not_called()


class MigrationAndOfflineRecoveryTests(unittest.TestCase):
    def test_migration_recovers_dangling_owned_link_and_local_old_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            share_root = network_share_manager.network_share_dir(settings)
            original = settings.roms_root / "snes.old"
            original.mkdir()
            (original / "local.zip").write_text("local")
            link = settings.roms_root / "snes"
            link.symlink_to(share_root / "peer-1" / "roms" / "snes", target_is_directory=True)

            result = network_share_manager.migrate_legacy_state(settings)

            self.assertTrue(result["migrated"])
            self.assertFalse(link.is_symlink())
            self.assertEqual((link / "local.zip").read_text(), "local")
            self.assertFalse(original.exists())
            self.assertEqual(network_share_manager._load_state(settings)["schema_version"], 3)

    def test_v2_transport_migration_does_not_unmount_or_rewrite_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            mount_point = network_share_manager.peer_mount_point(settings, "peer-1")
            local_link = settings.roms_root / "snes"
            local_link.symlink_to(mount_point / "roms" / "snes", target_is_directory=True)
            network_share_manager._save_state(
                settings,
                {
                    "schema_version": 2,
                    "peers": {
                        "peer-1": {
                            "peer_id": "peer-1",
                            "mount_point": str(mount_point),
                            "enabled": True,
                            "status": "mounted",
                        }
                    },
                },
            )

            with mock.patch.object(network_share_manager, "_unmount") as unmount, \
                    mock.patch.object(network_share_manager, "_recover_owned_orphaned_references") as recover:
                result = network_share_manager.migrate_legacy_state(settings)

            self.assertTrue(result["migrated"])
            unmount.assert_not_called()
            recover.assert_not_called()
            self.assertTrue(local_link.is_symlink())
            state = network_share_manager._load_state(settings)
            self.assertEqual(state["schema_version"], 3)
            self.assertEqual(state["peers"]["peer-1"]["protocol"], "smb")

    def test_disable_keeps_state_when_cleanup_is_not_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            network_share_manager._save_state(settings, {"schema_version": 2, "peers": {}})
            network_share_manager._upsert_peer_record(
                settings,
                "peer-1",
                peer_name="batocera",
                enabled=True,
                status="mounted",
                mount_point=str(network_share_manager.peer_mount_point(settings, "peer-1")),
                systems=[{"system": "snes", "symlink_created": True}],
                bios=[],
            )
            with mock.patch.object(network_share_manager, "_revert_system_references", return_value=["snes: denied"]), \
                    mock.patch.object(network_share_manager, "_unmount"):
                result = network_share_manager.disable(settings, "peer-1")
            self.assertEqual(result["status"], "error")
            record = network_share_manager.get_share(settings, "peer-1")
            self.assertIsNotNone(record)
            self.assertFalse(record["enabled"])


if __name__ == "__main__":
    unittest.main()
