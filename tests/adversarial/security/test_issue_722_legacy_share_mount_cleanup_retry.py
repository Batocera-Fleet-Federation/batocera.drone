"""Issue #722: a legacy network-share mount that will not unmount must stay retryable."""

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


class LegacyShareMountCleanupRetryTests(unittest.TestCase):
    def test_unmount_failure_keeps_share_record_for_retry(self) -> None:
        """On first start after upgrading, unmount any active network-share mounts. (Issue #722)"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "issue-722-share-retry",
            }
            with mock.patch.dict("os.environ", env, clear=True):
                settings = Settings.from_env()
                settings.roms_root.mkdir(parents=True, exist_ok=True)
                settings.bios_root.mkdir(parents=True, exist_ok=True)
                mount_point = root / "system" / "drone-network-shares" / "peer-a"
                (mount_point / "roms").mkdir(parents=True)
                db = database_path(settings.userdata_root)
                save_payload(
                    db,
                    "network_share_manager.json",
                    {"peers": {"peer-a": {"peer_id": "peer-a", "enabled": True, "mount_point": str(mount_point), "systems": [], "bios": []}}},
                )
                busy = mock.Mock(returncode=32, stdout="", stderr="target is busy")
                with mock.patch("app.device.legacy_reference_cleanup.subprocess.run", return_value=busy), \
                        mock.patch("app.device.legacy_reference_cleanup.shutil.which", side_effect=lambda n: f"/bin/{n}"), \
                        mock.patch("app.device.legacy_reference_cleanup.os.path.ismount", return_value=True):
                    legacy_reference_cleanup.cleanup_legacy_network_references(settings)
                remaining = (load_payload(db, "network_share_manager.json", {}) or {}).get("peers") or {}
            self.assertIn(
                "peer-a",
                remaining,
                "the mount is still attached but its state record was erased, so no later start can unmount it",
            )


if __name__ == "__main__":
    unittest.main()
