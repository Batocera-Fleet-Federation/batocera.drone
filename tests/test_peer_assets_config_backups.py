"""Tests for admin peer-assets config_backups local-existence annotation (#741)."""

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
            "DRONE_DEVICE_ID": "issue-741-target",
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


class PeerAssetsConfigBackupsFallbackTests(unittest.TestCase):
    def _call_peer_assets(self, handler: RomRequestHandler, items: list) -> tuple[int, dict]:
        peer = {"drone_id": "peer-1", "name": "Other Drone"}
        handler._peer_inventory_request = mock.Mock(
            return_value={"asset_type": "config_backups", "items": items, "total": len(items)}
        )
        with mock.patch.object(
            handlers_network._local_network, "is_local_mode", return_value=True
        ), mock.patch.object(
            handlers_network._local_network, "get_paired_peer", return_value=peer
        ):
            handler._handle_admin_local_peer_assets("peer-1", {"type": ["config_backups"]})
        handler._send_json.assert_called_once()
        status, payload = handler._send_json.call_args.args[:2]
        return status, payload

    def test_backups_directory_file_conflict_does_not_500_peer_assets_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            root.mkdir(parents=True)
            # A regular file at userdata_root/system makes backups_directory() raise OSError.
            (root / "system").write_text("not a directory", encoding="utf-8")
            settings = _settings(root)
            handler = _handler(settings)

            status, payload = self._call_peer_assets(
                handler,
                [{"name": "weekly", "file_name": "weekly.tar.gz", "relative_path": "weekly.tar.gz"}],
            )

            self.assertEqual(status, 200, payload)
            self.assertEqual(len(payload["items"]), 1)
            self.assertIs(payload["items"][0]["exists_locally"], False)

    def test_backups_directory_permission_or_setup_errors_degrade_to_not_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            for exc in (
                PermissionError("read-only userdata"),
                ValueError("invalid path"),
                RuntimeError("symlink loop"),
                TypeError("None userdata_root"),
            ):
                with self.subTest(exc=type(exc).__name__):
                    handler = _handler(settings)
                    with mock.patch.object(
                        handlers_network, "_config_backups_directory", side_effect=exc
                    ):
                        status, payload = self._call_peer_assets(
                            handler,
                            [{"name": "cabinet", "file_name": "cabinet.tar.gz"}],
                        )
                    self.assertEqual(status, 200, payload)
                    self.assertIs(payload["items"][0]["exists_locally"], False)

    def test_backups_directory_failure_with_empty_items_returns_200(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            handler = _handler(settings)
            with mock.patch.object(
                handlers_network,
                "_config_backups_directory",
                side_effect=OSError("disk unavailable"),
            ):
                status, payload = self._call_peer_assets(handler, [])
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["items"], [])

    def test_existing_and_missing_config_backups_annotated_accurately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            backups_dir = config_backup_mod.backups_directory(settings)
            (backups_dir / "present.tar.gz").write_bytes(b"backup")
            handler = _handler(settings)

            status, payload = self._call_peer_assets(
                handler,
                [
                    {"name": "present", "file_name": "present.tar.gz"},
                    {"name": "absent", "file_name": "absent.tar.gz"},
                ],
            )
            self.assertEqual(status, 200, payload)
            self.assertIs(payload["items"][0]["exists_locally"], True)
            self.assertIs(payload["items"][1]["exists_locally"], False)


if __name__ == "__main__":
    unittest.main()
