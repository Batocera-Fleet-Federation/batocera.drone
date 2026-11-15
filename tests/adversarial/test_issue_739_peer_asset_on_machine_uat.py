"""Issue #739: searching a peer's assets must mark ones already on this machine.

Quoted requirement: "When you search assets on a drone in the swarm (not your
own) and the asset is on the machine then it should indicate that the asset is
already on machine."

The swarm Request/Search Assets panel loads a peer inventory through
``_handle_admin_local_peer_assets``. Non-ROM types (BIOS, saves, movies,
config backups) must set ``exists_locally`` when the same file is already on
this Drone so the UI can show "On this machine".
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.device import config_backup as config_backup_mod
from app.drone_api import RomRequestHandler
from app.web import handlers_network


def _settings(root: Path) -> Settings:
    with mock.patch.dict(
        "os.environ",
        {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "MOVIES_ROOT": str(root / "movies"),
            "MUSIC_ROOT": str(root / "music"),
            "DRONE_DEVICE_ID": "issue-739-target",
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        },
        clear=True,
    ):
        return Settings.from_env()


def _handler(settings: Settings) -> RomRequestHandler:
    handler = object.__new__(RomRequestHandler)
    handler.settings = settings
    handler._send_json = mock.Mock()
    return handler


class PeerAssetOnMachineUat(unittest.TestCase):
    def _search(self, handler: RomRequestHandler, asset_type: str, items: list) -> dict:
        peer = {"drone_id": "peer-739", "name": "Other Drone"}
        handler._peer_inventory_request = mock.Mock(
            return_value={"asset_type": asset_type, "items": items, "total": len(items)}
        )
        with mock.patch.object(handlers_network._local_network, "is_local_mode", return_value=True), mock.patch.object(
            handlers_network._local_network, "get_paired_peer", return_value=peer
        ):
            handler._handle_admin_local_peer_assets("peer-739", {"type": [asset_type]})
        handler._send_json.assert_called()
        status, payload = handler._send_json.call_args.args[:2]
        self.assertEqual(status, 200, payload)
        return payload

    def test_bios_already_on_this_machine_is_flagged(self):
        # Issue #739: "When you search assets on a drone in the swarm (not your
        # own) and the asset is on the machine then it should indicate that the
        # asset is already on machine."
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = _settings(root)
            settings.bios_root.mkdir(parents=True)
            (settings.bios_root / "scph1001.bin").write_bytes(b"bios-bytes")
            handler = _handler(settings)
            payload = self._search(
                handler,
                "bios",
                [
                    {
                        "name": "scph1001.bin",
                        "path": "scph1001.bin",
                        "file_path": "scph1001.bin",
                        "relative_path": "scph1001.bin",
                    }
                ],
            )
            self.assertTrue(
                payload["items"][0].get("exists_locally") is True,
                "BIOS already stored under BIOS_ROOT must be flagged exists_locally "
                f"when browsing a peer: {payload['items'][0]!r}",
            )

    def test_save_already_on_this_machine_is_flagged(self):
        # Issue #739: "When you search assets on a drone in the swarm (not your
        # own) and the asset is on the machine then it should indicate that the
        # asset is already on machine."
        # Peer save inventory uses SaveEntry.to_payload(): file_path is relative
        # to saves_root and already includes the system directory.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = _settings(root)
            save_path = settings.saves_root / "snes" / "mario.srm"
            save_path.parent.mkdir(parents=True)
            save_path.write_bytes(b"save-bytes")
            handler = _handler(settings)
            payload = self._search(
                handler,
                "saves",
                [
                    {
                        "system": "snes",
                        "system_name": "snes",
                        "save_name": "mario.srm",
                        "name": "mario.srm",
                        "file_path": "snes/mario.srm",
                        "relative_path": "snes/mario.srm",
                    }
                ],
            )
            self.assertTrue(
                payload["items"][0].get("exists_locally") is True,
                "Save already stored under SAVES_ROOT must be flagged exists_locally "
                f"when browsing a peer: {payload['items'][0]!r}",
            )

    def test_movie_already_on_this_machine_is_flagged(self):
        # Issue #739: "When you search assets on a drone in the swarm (not your
        # own) and the asset is on the machine then it should indicate that the
        # asset is already on machine."
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = _settings(root)
            settings.movies_root.mkdir(parents=True)
            (settings.movies_root / "Film.mp4").write_bytes(b"movie-bytes")
            handler = _handler(settings)
            payload = self._search(
                handler,
                "movies",
                [
                    {
                        "name": "Film.mp4",
                        "file_path": "Film.mp4",
                        "relative_path": "Film.mp4",
                    }
                ],
            )
            self.assertTrue(
                payload["items"][0].get("exists_locally") is True,
                "Movie already stored under MOVIES_ROOT must be flagged exists_locally "
                f"when browsing a peer: {payload['items'][0]!r}",
            )

    def test_config_backup_already_on_this_machine_is_flagged(self):
        # Issue #739: "When you search assets on a drone in the swarm (not your
        # own) and the asset is on the machine then it should indicate that the
        # asset is already on machine."
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = _settings(root)
            backups = config_backup_mod.backups_directory(settings)
            (backups / "cabinet.tar.gz").write_bytes(b"backup-bytes")
            handler = _handler(settings)
            payload = self._search(
                handler,
                "config_backups",
                [
                    {
                        "name": "cabinet",
                        "file_name": "cabinet.tar.gz",
                        "relative_path": "cabinet.tar.gz",
                    }
                ],
            )
            self.assertTrue(
                payload["items"][0].get("exists_locally") is True,
                "Config backup already on this machine must be flagged exists_locally "
                f"when browsing a peer: {payload['items'][0]!r}",
            )


if __name__ == "__main__":
    unittest.main()
