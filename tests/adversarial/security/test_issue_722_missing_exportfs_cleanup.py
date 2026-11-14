"""Issue #722: unavailable tools do not prove legacy NFS grants were revoked."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.device import legacy_reference_cleanup as cleanup
from app.storage.state_store import load_payload, save_payload


class MissingExportfsCleanupTests(unittest.TestCase):
    def test_missing_exportfs_keeps_client_address_until_revocation_can_be_verified(self):
        """'Removed handlers must not leave exports open.' Confidence: medium."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_NFS_EXPORT_ROOT": str(root / "nfs-export"),
                "DRONE_DEVICE_ID": "issue-722-missing-exportfs",
            }
            with mock.patch.dict("os.environ", env, clear=True):
                settings = Settings.from_env()
                database = settings.state_database_file
                peer = {"addresses": ["192.168.1.44"], "status": "active"}
                save_payload(database, "nfs_export_manager.json", {"peers": {"peer-a": peer}})
                # An upgraded userland may temporarily lack exportfs while the
                # already-running kernel NFS server and a busy bind remain.
                # No real mounts, subprocesses, or remote hosts are touched.
                with mock.patch.object(cleanup, "_find_command", return_value=None), \
                        mock.patch.object(cleanup, "_mounted_descendants", return_value=[root / "nfs-export/bios"]), \
                        mock.patch.object(cleanup, "_unmount_path", return_value=False), \
                        mock.patch.object(cleanup.subprocess, "run") as run:
                    cleanup._cleanup_nfs_exports(settings, database, "fixture-umount")
                run.assert_not_called()
                remaining = load_payload(database, "nfs_export_manager.json", {})
                # Restore the tool and restart: the original grant must still
                # be known, so cleanup actually attempts to revoke it.
                with mock.patch.object(cleanup, "_find_command", return_value="fixture-exportfs"), \
                        mock.patch.object(cleanup, "_mounted_descendants", return_value=[]), \
                        mock.patch.object(cleanup, "_unmount_path", return_value=True), \
                        mock.patch.object(cleanup, "_revoke_address", return_value=True) as revoke:
                    cleanup._cleanup_nfs_exports(settings, database, "fixture-umount")
                self.assertEqual(
                    remaining.get("peers", {}).get("peer-a", {}).get("addresses"),
                    ["192.168.1.44"],
                    "Missing exportfs is not evidence that a kernel grant disappeared",
                )
                revoke.assert_called_once_with("fixture-exportfs", "192.168.1.44", root / "nfs-export")


if __name__ == "__main__":
    unittest.main()
