"""Focused tests for one-time legacy Reference ROMs / NFS export cleanup on upgrade."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.device import legacy_reference_cleanup
from app.storage.state_store import database_path, load_payload, save_payload


class LegacyReferenceCleanupTests(unittest.TestCase):
    def test_unmounts_shares_restores_local_systems_and_revokes_nfs_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            roms = root / "roms"
            bios = root / "bios"
            roms.mkdir(parents=True)
            bios.mkdir(parents=True)
            with mock.patch.dict(
                "os.environ",
                {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(roms), "BIOS_ROOT": str(bios)},
                clear=True,
            ):
                settings = Settings.from_env()

            mount_point = root / "system" / "drone-network-shares" / "peer-legacy"
            (mount_point / "roms" / "snes").mkdir(parents=True)
            (mount_point / "bios").mkdir(parents=True)
            (mount_point / "bios" / "scph5501.bin").write_bytes(b"remote-bios")

            # Simulated local SNES folder that had been renamed aside to snes.old
            preserved_snes = roms / "snes.old"
            preserved_snes.mkdir(parents=True)
            (preserved_snes / "SuperMarioWorld.sfc").write_bytes(b"local-rom")
            (roms / "snes").symlink_to(mount_point / "roms" / "snes", target_is_directory=True)
            (bios / "scph5501.bin").symlink_to(mount_point / "bios" / "scph5501.bin")

            db = database_path(settings.userdata_root)
            save_payload(
                db,
                "network_share_manager.json",
                {
                    "peers": {
                        "peer-legacy": {
                            "peer_id": "peer-legacy",
                            "enabled": True,
                            "status": "mounted",
                            "mount_point": str(mount_point),
                            "systems": [
                                {
                                    "system": "snes",
                                    "had_local_collision": True,
                                    "renamed_to": "snes.old",
                                    "symlink_created": True,
                                }
                            ],
                            "bios_files": [
                                {
                                    "relative_path": "scph5501.bin",
                                    "had_local_collision": False,
                                    "symlink_created": True,
                                }
                            ],
                        }
                    }
                },
            )
            save_payload(
                db,
                "nfs_export_manager.json",
                {
                    "peers": {
                        "peer-consumer": {
                            "addresses": ["192.168.1.44", "100.64.0.9"],
                        }
                    }
                },
            )
            save_payload(
                db,
                "network_reference_selection.json",
                {"peer_id": "peer-legacy", "selected_systems": ["snes"]},
            )

            commands: list[list[str]] = []

            def fake_run(cmd, **_kwargs):
                commands.append([str(part) for part in cmd])
                if str(cmd[0]).endswith("umount"):
                    for child in sorted(mount_point.rglob("*"), reverse=True):
                        if child.is_file() or child.is_symlink():
                            child.unlink(missing_ok=True)
                        elif child.is_dir():
                            child.rmdir()
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch("app.device.legacy_reference_cleanup.subprocess.run", side_effect=fake_run), mock.patch(
                "app.device.legacy_reference_cleanup.shutil.which",
                side_effect=lambda name: f"/usr/sbin/{name}",
            ):
                legacy_reference_cleanup.cleanup_legacy_network_references(settings)

            self.assertFalse((roms / "snes").is_symlink())
            self.assertTrue((roms / "snes" / "SuperMarioWorld.sfc").is_file())
            self.assertFalse(preserved_snes.exists())
            self.assertFalse((bios / "scph5501.bin").exists())
            self.assertFalse(mount_point.exists())
            self.assertEqual((load_payload(db, "network_share_manager.json", {}) or {}).get("peers"), {})
            self.assertEqual((load_payload(db, "nfs_export_manager.json", {}) or {}).get("peers"), {})
            self.assertEqual(load_payload(db, "network_reference_selection.json", {}), {})
            self.assertTrue(any(str(cmd[0]).endswith("exportfs") and "-u" in cmd for cmd in commands))


    def test_default_export_root_is_the_historical_install_root_tree(self) -> None:
        from app.common.install_paths import drone_install_root

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(legacy_reference_cleanup._export_root(), drone_install_root() / "nfs-export")

    def test_failed_export_revocation_keeps_only_failed_addresses_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_NFS_EXPORT_ROOT": str(root / "nfs-export"),
            }
            with mock.patch.dict("os.environ", env, clear=True):
                settings = Settings.from_env()
                db = database_path(settings.userdata_root)
                save_payload(db, "nfs_export_manager.json", {"peers": {"p": {"addresses": ["192.168.1.1", "192.168.1.2"]}}})

                def fake_run(cmd, **_kwargs):
                    bad = "-s" in cmd or any("192.168.1.2:" in str(part) for part in cmd)
                    return mock.Mock(returncode=1 if bad else 0, stdout="", stderr="")

                with mock.patch("app.device.legacy_reference_cleanup.subprocess.run", side_effect=fake_run), mock.patch(
                    "app.device.legacy_reference_cleanup.shutil.which", side_effect=lambda n: f"/usr/sbin/{n}"
                ):
                    legacy_reference_cleanup.cleanup_legacy_network_references(settings)
                peers = (load_payload(db, "nfs_export_manager.json", {}) or {}).get("peers")
                self.assertEqual(peers, {"p": {"addresses": ["192.168.1.2"]}})

    def test_missing_exportfs_keeps_ownership_record_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_NFS_EXPORT_ROOT": str(root / "nfs-export"),
            }
            with mock.patch.dict("os.environ", env, clear=True):
                settings = Settings.from_env()
                db = database_path(settings.userdata_root)
                save_payload(db, "nfs_export_manager.json", {"peers": {"p": {"addresses": ["192.168.1.9"]}}})
                with mock.patch.object(legacy_reference_cleanup, "_find_command", return_value=None):
                    legacy_reference_cleanup._cleanup_nfs_exports(settings, db, "umount")
                state = load_payload(db, "nfs_export_manager.json", {}) or {}
                self.assertEqual(state.get("peers"), {"p": {"addresses": ["192.168.1.9"]}})
                # Once exportfs is available the retained grant is revoked and cleared.
                with mock.patch.object(legacy_reference_cleanup, "_find_command", return_value="/usr/sbin/exportfs"), \
                        mock.patch.object(legacy_reference_cleanup, "_revoke_address", return_value=True) as revoke:
                    legacy_reference_cleanup._cleanup_nfs_exports(settings, db, "umount")
                revoke.assert_called_once()
                state = load_payload(db, "nfs_export_manager.json", {}) or {}
                self.assertEqual(state.get("peers"), {})


if __name__ == "__main__":
    unittest.main()
