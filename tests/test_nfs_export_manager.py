"""Source-side NFS export authorization and cleanup tests."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.device import nfs_export_manager


def _build_settings(test_case: unittest.TestCase, root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "nfs-export-test",
    }
    install_root = root / "install-root"
    patcher = mock.patch.object(nfs_export_manager, "_drone_install_root", return_value=install_root)
    test_case.addCleanup(patcher.stop)
    patcher.start()
    with mock.patch.dict("os.environ", env, clear=True):
        settings = Settings.from_env()
    settings.roms_root.mkdir(parents=True, exist_ok=True)
    settings.bios_root.mkdir(parents=True, exist_ok=True)
    return settings


_CAPABILITY = {
    "available": True,
    "protocol": "nfs",
    "versions": ["4.2", "4.1", "4"],
    "preferred_version": "4.2",
    "port": 2049,
    "detail": "",
}


class NfsCapabilityTests(unittest.TestCase):
    def test_capabilities_prefers_highest_active_nfs4_version(self) -> None:
        with mock.patch.object(nfs_export_manager, "_find_command", side_effect=lambda name: f"/sbin/{name}"), \
                mock.patch.object(nfs_export_manager, "_nfs_server_versions", return_value=["4.2", "4.1", "4"]):
            result = nfs_export_manager.capabilities()

        self.assertTrue(result["available"])
        self.assertEqual(result["preferred_version"], "4.2")

    def test_capabilities_reports_missing_server_tools(self) -> None:
        with mock.patch.object(nfs_export_manager, "_find_command", return_value=None), \
                mock.patch.object(nfs_export_manager, "_nfs_server_versions", return_value=[]):
            result = nfs_export_manager.capabilities()

        self.assertFalse(result["available"])
        self.assertIn("exportfs", result["detail"])
        self.assertIn("NFSv4", result["detail"])

    def test_only_private_lan_and_tailscale_ipv4_addresses_are_authorizable(self) -> None:
        self.assertEqual(nfs_export_manager._normalized_client_ipv4("192.168.10.4"), "192.168.10.4")
        self.assertEqual(nfs_export_manager._normalized_client_ipv4("100.91.173.37"), "100.91.173.37")
        self.assertIsNone(nfs_export_manager._normalized_client_ipv4("8.8.8.8"))
        self.assertIsNone(nfs_export_manager._normalized_client_ipv4("127.0.0.1"))
        self.assertIsNone(nfs_export_manager._normalized_client_ipv4("fd00::1"))


class NfsAuthorizationTests(unittest.TestCase):
    def test_authorize_uses_only_paired_peer_addresses_and_persists_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            peer = {
                "drone_id": "peer-1",
                "name": "Laptop",
                "source_ip": "192.168.0.178",
                "tailnet_ip": "100.91.173.37",
            }
            with mock.patch.object(nfs_export_manager._local_network, "get_paired_peer", return_value=peer), \
                    mock.patch.object(nfs_export_manager, "capabilities", return_value=dict(_CAPABILITY)), \
                    mock.patch.object(nfs_export_manager, "_ensure_export_root"), \
                    mock.patch.object(nfs_export_manager, "_export_client") as export_client:
                result = nfs_export_manager.authorize_peer(settings, "peer-1", "192.168.0.178")

            self.assertEqual(result["export_path"], "/")
            self.assertEqual(result["authorized_addresses"], ["192.168.0.178", "100.91.173.37"])
            self.assertEqual(
                [call.args[1] for call in export_client.call_args_list],
                ["192.168.0.178", "100.91.173.37"],
            )
            record = nfs_export_manager._load_state(settings)["peers"]["peer-1"]
            self.assertEqual(record["status"], "active")
            self.assertEqual(record["addresses"], ["192.168.0.178", "100.91.173.37"])

    def test_authorize_rejects_an_unpaired_caller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(nfs_export_manager._local_network, "get_paired_peer", return_value=None):
                with self.assertRaisesRegex(ValueError, "paired"):
                    nfs_export_manager.authorize_peer(settings, "stranger", "192.168.0.44")

    def test_reauthorization_removes_addresses_no_longer_owned_by_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            nfs_export_manager._save_state(
                settings,
                {"peers": {"peer-1": {"peer_id": "peer-1", "addresses": ["192.168.0.178"]}}},
            )
            peer = {"drone_id": "peer-1", "tailnet_ip": "100.91.173.37"}
            with mock.patch.object(nfs_export_manager._local_network, "get_paired_peer", return_value=peer), \
                    mock.patch.object(nfs_export_manager, "capabilities", return_value=dict(_CAPABILITY)), \
                    mock.patch.object(nfs_export_manager, "_ensure_export_root"), \
                    mock.patch.object(nfs_export_manager, "_export_client"), \
                    mock.patch.object(nfs_export_manager, "_unexport_client", return_value=None) as unexport:
                nfs_export_manager.authorize_peer(settings, "peer-1", "100.91.173.37")

            unexport.assert_called_once_with(settings, "192.168.0.178")
            self.assertEqual(
                nfs_export_manager._load_state(settings)["peers"]["peer-1"]["addresses"],
                ["100.91.173.37"],
            )

    def test_revoke_keeps_an_address_still_used_by_another_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            nfs_export_manager._save_state(
                settings,
                {
                    "peers": {
                        "peer-1": {"addresses": ["192.168.0.178", "100.91.173.37"]},
                        "peer-2": {"addresses": ["192.168.0.178"]},
                    }
                },
            )
            with mock.patch.object(nfs_export_manager, "_unexport_client", return_value=None) as unexport, \
                    mock.patch.object(nfs_export_manager, "_cleanup_bind_mounts") as cleanup:
                result = nfs_export_manager.revoke_peer(settings, "peer-1")

            self.assertEqual(result["status"], "revoked")
            unexport.assert_called_once_with(settings, "100.91.173.37")
            cleanup.assert_not_called()
            self.assertIn("peer-2", nfs_export_manager._load_state(settings)["peers"])

    def test_restore_removes_unpaired_authorizations_and_replays_paired_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            nfs_export_manager._save_state(
                settings,
                {
                    "peers": {
                        "gone": {"addresses": ["192.168.0.40"]},
                        "peer-2": {"addresses": ["100.64.0.22"]},
                    }
                },
            )
            with mock.patch.object(
                nfs_export_manager._local_network,
                "paired_peers",
                return_value=[{"drone_id": "peer-2"}],
            ), mock.patch.object(nfs_export_manager, "_ensure_export_root"), \
                    mock.patch.object(nfs_export_manager, "_unexport_client", return_value=None) as unexport, \
                    mock.patch.object(nfs_export_manager, "_export_client") as export_client:
                nfs_export_manager.restore_exports(settings)

            unexport.assert_called_once_with(settings, "192.168.0.40")
            export_client.assert_called_once_with(settings, "100.64.0.22")
            state = nfs_export_manager._load_state(settings)
            self.assertNotIn("gone", state["peers"])
            self.assertEqual(state["peers"]["peer-2"]["status"], "active")

    def test_restore_retains_failed_unpaired_cleanup_without_reexporting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            nfs_export_manager._save_state(
                settings,
                {"peers": {"gone": {"addresses": ["192.168.0.40"]}}},
            )
            with mock.patch.object(nfs_export_manager._local_network, "paired_peers", return_value=[]), \
                    mock.patch.object(nfs_export_manager, "_unexport_client", return_value="exportfs failed"), \
                    mock.patch.object(nfs_export_manager, "_export_client") as export_client, \
                    mock.patch.object(nfs_export_manager, "_cleanup_bind_mounts") as cleanup:
                nfs_export_manager.restore_exports(settings)

            export_client.assert_not_called()
            cleanup.assert_called_once_with(settings)
            record = nfs_export_manager._load_state(settings)["peers"]["gone"]
            self.assertEqual(record["status"], "error")
            self.assertIn("exportfs failed", record["status_detail"])


class NfsBindMountTests(unittest.TestCase):
    def test_bind_mount_uses_exact_source_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "roms"
            source.mkdir()
            target = root / "export" / "roms"
            with mock.patch.object(nfs_export_manager, "_is_mounted", return_value=False), \
                    mock.patch.object(nfs_export_manager, "_find_command", return_value="/bin/mount"), \
                    mock.patch.object(
                        nfs_export_manager,
                        "_run",
                        return_value=mock.Mock(returncode=0, stdout="", stderr=""),
                    ) as run:
                nfs_export_manager._ensure_bind_mount(source, target)

            run.assert_called_once_with(["/bin/mount", "--bind", str(source), str(target)])

    def test_resolved_rom_sources_follow_external_links_and_skip_unsafe_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(self, root)
            external_snes = root / "external" / "Super Nintendo"
            external_snes.mkdir(parents=True)
            (settings.roms_root / "snes").symlink_to(external_snes, target_is_directory=True)
            local_nes = settings.roms_root / "nes"
            local_nes.mkdir()
            (settings.roms_root / "broken").symlink_to(root / "missing", target_is_directory=True)
            (settings.roms_root / "genesis.old").mkdir()
            network_root = root / "network-shares"
            network_psx = network_root / "peer" / "roms" / "psx"
            network_psx.mkdir(parents=True)
            (settings.roms_root / "psx").symlink_to(network_psx, target_is_directory=True)

            with mock.patch.object(nfs_export_manager, "network_reference_root", return_value=network_root):
                sources = nfs_export_manager._resolved_rom_sources(settings)

            self.assertEqual(sources, {"nes": local_nes.resolve(), "snes": external_snes.resolve()})

    def test_resolved_rom_export_binds_each_real_system_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(self, root)
            external_snes = root / "external" / "Super Nintendo"
            external_snes.mkdir(parents=True)
            (settings.roms_root / "snes").symlink_to(external_snes, target_is_directory=True)
            local_nes = settings.roms_root / "nes"
            local_nes.mkdir()
            export_root = nfs_export_manager.export_root(settings)

            with mock.patch.object(nfs_export_manager, "_is_mounted", return_value=False), \
                    mock.patch.object(nfs_export_manager, "_mounted_descendants", return_value=[]), \
                    mock.patch.object(nfs_export_manager, "_find_command", return_value="/bin/mount"), \
                    mock.patch.object(
                        nfs_export_manager,
                        "_run",
                        return_value=mock.Mock(returncode=0, stdout="", stderr=""),
                    ) as run:
                nfs_export_manager._ensure_resolved_rom_export(settings, export_root)

            calls = [call.args[0] for call in run.call_args_list]
            self.assertEqual(
                calls,
                [
                    ["/bin/mount", "--bind", str(local_nes.resolve()), str(export_root / "roms" / "nes")],
                    ["/bin/mount", "--bind", str(external_snes.resolve()), str(export_root / "roms" / "snes")],
                ],
            )
            self.assertNotIn(
                ["/bin/mount", "--bind", str(settings.roms_root), str(export_root / "roms")],
                calls,
            )

    def test_export_assigns_explicit_distinct_fsids_to_root_roms_and_bios(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            root = nfs_export_manager.export_root(settings)
            with mock.patch.object(nfs_export_manager, "_find_command", return_value="/sbin/exportfs"), \
                    mock.patch.object(
                        nfs_export_manager,
                        "_run",
                        return_value=mock.Mock(returncode=0, stdout="", stderr=""),
                    ) as run:
                nfs_export_manager._export_client(settings, "100.64.0.22")

            calls = [call.args[0] for call in run.call_args_list]
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[0][-1], f"100.64.0.22:{root}")
            self.assertIn("fsid=0", calls[0][calls[0].index("-o") + 1].split(","))
            child_options = {
                args[-1].rsplit("/", 1)[-1]: args[args.index("-o") + 1]
                for args in calls[1:]
            }
            self.assertEqual(set(child_options), {"roms", "bios"})
            self.assertNotEqual(child_options["roms"], child_options["bios"])
            self.assertTrue(all("root_squash" in options.split(",") for options in child_options.values()))
            self.assertIn("crossmnt", child_options["roms"].split(","))
            self.assertNotIn("crossmnt", child_options["bios"].split(","))

    def test_unexport_removes_children_before_the_nfs4_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            root = nfs_export_manager.export_root(settings)
            with mock.patch.object(nfs_export_manager, "_find_command", return_value="/sbin/exportfs"), \
                    mock.patch.object(
                        nfs_export_manager,
                        "_run",
                        return_value=mock.Mock(returncode=0, stdout="", stderr=""),
                    ) as run:
                error = nfs_export_manager._unexport_client(settings, "100.64.0.22")

            self.assertIsNone(error)
            targets = [call.args[0][-1] for call in run.call_args_list]
            self.assertEqual(targets[-1], f"100.64.0.22:{root}")
            self.assertEqual(set(targets[:-1]), {f"100.64.0.22:{root / 'roms'}", f"100.64.0.22:{root / 'bios'}"})


if __name__ == "__main__":
    unittest.main()
