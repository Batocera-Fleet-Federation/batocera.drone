"""Issue #722: a failed upgrade-time NFS export revocation must stay retryable."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.common.settings import Settings  # noqa: E402
from app.device import legacy_reference_cleanup  # noqa: E402
from app.storage.state_store import database_path, load_payload, save_payload  # noqa: E402


def _settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "issue-722-nfs-retry",
        "DRONE_NFS_EXPORT_ROOT": str(root / "nfs-export"),
    }
    with mock.patch.dict("os.environ", env, clear=True):
        settings = Settings.from_env()
        settings.roms_root.mkdir(parents=True, exist_ok=True)
        settings.bios_root.mkdir(parents=True, exist_ok=True)
        return settings


class LegacyNfsExportCleanupRetryTests(unittest.TestCase):
    def test_failed_exportfs_revocation_keeps_ownership_record_for_retry(self) -> None:
        """Removed handlers must not leave exports open. (Issue #722 upgrade safety)"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _settings(root)
            env = {"DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"), "DRONE_NFS_EXPORT_ROOT": str(root / "nfs-export")}
            with mock.patch.dict("os.environ", env, clear=False):
                db = database_path(settings.userdata_root)
                save_payload(
                    db,
                    "nfs_export_manager.json",
                    {"peers": {"peer-a": {"addresses": ["192.168.1.44"]}}},
                )

                def failing_run(cmd, **_kwargs):
                    return mock.Mock(returncode=1, stdout="", stderr="exportfs: busy")

                with mock.patch("app.device.legacy_reference_cleanup.subprocess.run", side_effect=failing_run), \
                        mock.patch("app.device.legacy_reference_cleanup.shutil.which", side_effect=lambda n: f"/usr/sbin/{n}"):
                    legacy_reference_cleanup.cleanup_legacy_network_references(settings)

                remaining = (load_payload(db, "nfs_export_manager.json", {}) or {}).get("peers") or {}
            self.assertIn(
                "peer-a",
                remaining,
                "exportfs -u failed, yet the only record of the still-open export was erased; "
                "the next start can never retry the revocation",
            )


if __name__ == "__main__":
    unittest.main()
