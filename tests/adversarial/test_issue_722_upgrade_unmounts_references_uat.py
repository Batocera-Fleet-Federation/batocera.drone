"""Issue #722: upgrading a Drone with a live reference unmounts it and drops exports.

Quoted requirements:
- "On first start after upgrading, unmount any active network-share mounts and remove their mount points and state files. They must not be left as stale mounts under `/userdata/roms`."
- "On the source side, remove any NFS exports the Drone created for peers (`/etc/exports` entries / `exportfs`). Removed handlers must not leave exports open."
- "the network-share watchdog poller (`drone_api.py` / `runtime_state.py`)"
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
from app.common.settings import Settings
from app.storage.state_store import load_payload, save_payload
from app.transfer.peer_workers import stop_local_network_workers


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


class UpgradeUnmountsReferencesUat(unittest.TestCase):
    def test_first_start_unmounts_references_and_removes_exports(self) -> None:
        # "On first start after upgrading, unmount any active network-share mounts and remove their mount points and state files."
        # "On the source side, remove any NFS exports the Drone created for peers (`/etc/exports` entries / `exportfs`)."
        import tempfile

        recorded: list[list[str]] = []
        real_run = subprocess.run
        real_ismount = os.path.ismount

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            userdata = root / "userdata"
            roms = root / "roms"
            share_root = root / "network-shares"
            export_root = root / "nfs-export"
            mount_point = share_root / "peer-old"
            (mount_point / "roms" / "gb").mkdir(parents=True)
            roms.mkdir()
            link = roms / "gb"
            link.symlink_to(mount_point / "roms" / "gb", target_is_directory=True)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for name in ("exportfs", "umount", "mount"):
                program = bin_dir / name
                program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                program.chmod(0o755)

            env = {
                "USERDATA_ROOT": str(userdata),
                "ROMS_ROOT": str(roms),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "issue-722-upgrade",
                "HTTPS_PORT": str(_free_port()),
                "DRONE_PEER_MTLS_PORT": str(_free_port()),
                "HTTP_REDIRECT_PORT": "0",
                "DRONE_CAST_ENABLED": "0",
                "ROM_METADATA_POLL_SECONDS": "0",
                "HTTP_ONLY": "1",
                "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1",
                "DRONE_NETWORK_SHARE_DIR": str(share_root),
                "DRONE_NFS_EXPORT_ROOT": str(export_root),
                "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                "ES_SETTINGS_FILE": str(root / "es_settings.cfg"),
                "ES_SYSTEMS_FILE": str(root / "es_systems.cfg"),
            }
            mount_text = str(mount_point)

            def ismount(path) -> bool:
                if os.path.abspath(str(path)) == os.path.abspath(mount_text):
                    return not any(
                        Path(command[0]).name == "umount" and mount_text in command
                        for command in recorded
                    )
                return real_ismount(path)

            def run(args, *pos, **kwargs):
                command = [str(part) for part in (args if isinstance(args, (list, tuple)) else [args])]
                recorded.append(command)
                name = Path(command[0]).name
                if name in {"exportfs", "umount", "mount", "showmount"}:
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                return real_run(args, *pos, **kwargs)

            with mock.patch.dict(os.environ, env, clear=False):
                settings = Settings.from_env()
                settings.roms_root.mkdir(parents=True, exist_ok=True)
                settings.bios_root.mkdir(parents=True, exist_ok=True)
                database = settings.state_database_file
                save_payload(
                    database,
                    "network_share_manager.json",
                    {
                        "schema_version": 1,
                        "peers": {
                            "peer-old": {
                                "peer_id": "peer-old",
                                "peer_name": "living-room",
                                "enabled": True,
                                "status": "mounted",
                                "mount_point": mount_text,
                                "protocol": "nfs",
                                "systems": [{"system": "gb", "symlink_created": True}],
                            }
                        },
                    },
                )
                save_payload(
                    database,
                    "nfs_export_manager.json",
                    {
                        "schema_version": 1,
                        "peers": {
                            "peer-old": {
                                "peer_id": "peer-old",
                                "addresses": ["10.1.2.3"],
                                "status": "active",
                            }
                        },
                    },
                )
                for flag in (
                    "_NFS_EXPORT_BOOT_REPLAY_ATTEMPTED",
                    "_NETWORK_SHARE_BOOT_REPLAY_ATTEMPTED",
                    "_NETWORK_SHARE_WATCHDOG_STARTED",
                ):
                    if hasattr(drone_api, flag):
                        setattr(drone_api, flag, False)
                before = {thread.ident for thread in threading.enumerate()}
                server = None
                try:
                    with mock.patch("os.path.ismount", side_effect=ismount), \
                            mock.patch("pathlib.Path.is_mount", ismount), \
                            mock.patch("subprocess.run", side_effect=run), \
                            mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=True):
                        server = drone_api.create_server(settings)
                        spawned = [thread for thread in threading.enumerate() if thread.ident not in before]
                        watchdog = [
                            thread for thread in spawned
                            if "network-share" in thread.name and "watchdog" in thread.name
                        ]
                        self.assertEqual(
                            [thread.name for thread in watchdog],
                            [],
                            "network-share watchdog poller is still started",
                        )
                        for thread in spawned:
                            if any(token in thread.name for token in ("watchdog", "poller", "revocation", "self-heal", "listener")):
                                continue
                            thread.join(timeout=5)
                finally:
                    drone_api.retire_background_pollers()
                    stop_local_network_workers(timeout=1.0)
                    if server is not None:
                        for listener in list(getattr(server, "all_tls_servers", ()) or ()):
                            worker = getattr(listener, "thread", None)
                            if worker is not None:
                                listener.shutdown()
                                worker.join(timeout=3)
                            listener.server_close()
                        if getattr(server, "thread", None) is None:
                            server.server_close()

            names = [Path(command[0]).name for command in recorded]
            umounts = [command for command in recorded if Path(command[0]).name == "umount" and mount_text in command]
            exports_removed = [
                command for command in recorded
                if Path(command[0]).name == "exportfs" and "-u" in command and any("10.1.2.3" in part for part in command)
            ]
            exports_added = [
                command for command in recorded
                if Path(command[0]).name == "exportfs" and ("-i" in command or "-o" in command)
            ]
            self.assertTrue(umounts, f"active reference was not unmounted; commands={names}")
            self.assertFalse(link.exists() or link.is_symlink(), "stale ROM reference symlink is still under the ROM library")
            self.assertFalse(mount_point.exists(), "network-share mount point was left behind")
            self.assertTrue(exports_removed, f"NFS export for 10.1.2.3 was not revoked; commands={recorded}")
            self.assertEqual(exports_added, [], f"upgrade re-created NFS exports: {exports_added}")
            share_state = load_payload(database, "network_share_manager.json", {"missing": True})
            export_state = load_payload(database, "nfs_export_manager.json", {"missing": True})
            share_peers = share_state.get("peers") if isinstance(share_state, dict) else None
            export_peers = export_state.get("peers") if isinstance(export_state, dict) else None
            self.assertTrue(share_state == {"missing": True} or not share_peers, share_state)
            self.assertTrue(export_state == {"missing": True} or not export_peers, export_state)


if __name__ == "__main__":
    unittest.main()
