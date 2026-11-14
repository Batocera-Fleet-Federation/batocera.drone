"""Issue #722: first-start cleanup must revoke the NFS exports this Drone actually created.

Quoted requirement:
- "On the source side, remove any NFS exports the Drone created for peers (`/etc/exports` entries / `exportfs`). Removed handlers must not leave exports open."

Field Drones exported ``drone_install_root()/nfs-export`` (and its ``roms`` and ``bios``
children) with ``exportfs -u``. The override ``DRONE_NFS_EXPORT_ROOT`` is optional and
is unset on an ordinary upgrade.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.install_paths import drone_install_root
from app.common.settings import Settings
from app.device import legacy_reference_cleanup
from app.storage.state_store import load_payload, save_payload


class HistoricalNfsExportRootUat(unittest.TestCase):
    def test_unset_export_root_unexports_the_install_root_nfs_tree(self) -> None:
        # "On the source side, remove any NFS exports the Drone created for peers (`/etc/exports` entries / `exportfs`). Removed handlers must not leave exports open."
        historical_root = drone_install_root() / "nfs-export"
        address = "10.4.5.6"
        commands: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            commands.append([str(part) for part in cmd])
            return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            userdata = root / "userdata"
            roms = root / "roms"
            bios = root / "bios"
            saves = root / "saves"
            for path in (userdata, roms, bios, saves):
                path.mkdir()
            database = root / "state.sqlite3"
            env = {
                "USERDATA_ROOT": str(userdata),
                "ROMS_ROOT": str(roms),
                "BIOS_ROOT": str(bios),
                "SAVES_ROOT": str(saves),
                "DRONE_STATE_DATABASE_FILE": str(database),
                "DRONE_DEVICE_ID": "issue-722-nfs-root",
            }
            self.assertNotIn("DRONE_NFS_EXPORT_ROOT", env)
            with mock.patch.dict(os.environ, env, clear=True), mock.patch(
                "app.device.legacy_reference_cleanup.subprocess.run", side_effect=fake_run
            ), mock.patch(
                "app.device.legacy_reference_cleanup.shutil.which",
                side_effect=lambda name: f"/usr/sbin/{name}",
            ):
                self.assertFalse(os.environ.get("DRONE_NFS_EXPORT_ROOT", "").strip())
                settings = Settings.from_env()
                save_payload(
                    settings.state_database_file,
                    "nfs_export_manager.json",
                    {"schema_version": 1, "peers": {"older-peer": {"addresses": [address]}}},
                )
                legacy_reference_cleanup.cleanup_legacy_network_references(settings)
                remaining = load_payload(settings.state_database_file, "nfs_export_manager.json", {})

        revoked = []
        for command in commands:
            if "-u" not in command:
                continue
            if not any(part == "exportfs" or part.endswith("/exportfs") for part in command):
                continue
            for part in command:
                prefix = address + ":"
                if part.startswith(prefix):
                    revoked.append(part[len(prefix):])

        self.assertIn(str(historical_root), revoked, commands)
        self.assertIn(str(historical_root / "roms"), revoked, commands)
        self.assertIn(str(historical_root / "bios"), revoked, commands)
        self.assertNotIn(str(Path("/var/run/drone-nfs-export")), revoked, commands)
        self.assertEqual((remaining or {}).get("peers"), {})


if __name__ == "__main__":
    unittest.main()
