"""Issue #739: searching a peer's movies/shows must flag TV shows already in SHOWS_ROOT.

Quoted requirement: "When you search assets on a drone in the swarm (not your
own) and the asset is on the machine then it should indicate that the asset is
already on machine."

Peer ``movies`` inventories include TV shows under the ``Shows/`` virtual prefix
(stored locally under ``Settings.shows_root``). When that file already exists
under ``shows_root``, ``_handle_admin_local_peer_assets`` must flag it
``exists_locally=True``.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
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
            "SHOWS_ROOT": str(root / "shows"),
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


class PeerShowOnMachineUat(unittest.TestCase):
    def test_tv_show_in_shows_root_is_flagged_exists_locally(self):
        # Issue #739: "When you search assets on a drone in the swarm (not your
        # own) and the asset is on the machine then it should indicate that the
        # asset is already on machine."
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = _settings(root)
            local_show = settings.shows_root / "MyShow" / "S01E01.mkv"
            local_show.parent.mkdir(parents=True)
            local_show.write_bytes(b"episode-bytes")

            handler = _handler(settings)
            peer = {"drone_id": "peer-739", "name": "Other Drone"}
            items = [
                {
                    "name": "S01E01",
                    "movie_name": "S01E01",
                    "file_path": "Shows/MyShow/S01E01.mkv",
                    "relative_path": "Shows/MyShow/S01E01.mkv",
                },
                {
                    "name": "S01E02",
                    "movie_name": "S01E02",
                    "file_path": "Shows/MyShow/S01E02.mkv",
                    "relative_path": "Shows/MyShow/S01E02.mkv",
                },
            ]
            handler._peer_inventory_request = mock.Mock(
                return_value={"asset_type": "movies", "items": items, "total": len(items)}
            )
            with mock.patch.object(handlers_network._local_network, "is_local_mode", return_value=True), mock.patch.object(
                handlers_network._local_network, "get_paired_peer", return_value=peer
            ):
                handler._handle_admin_local_peer_assets("peer-739", {"type": ["movies"]})

            handler._send_json.assert_called()
            status, payload = handler._send_json.call_args.args[:2]
            self.assertEqual(status, 200, payload)
            self.assertTrue(
                payload["items"][0].get("exists_locally") is True,
                "TV show already stored under SHOWS_ROOT must be flagged exists_locally "
                f"when browsing a peer's movies inventory: {payload['items'][0]!r}",
            )
            self.assertIs(payload["items"][1].get("exists_locally"), False)


if __name__ == "__main__":
    unittest.main()
