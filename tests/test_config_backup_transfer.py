"""Config-backup P2P transfer + email: the peer-serve handler, the peer
download function, peer inventory listing, local-network sync dispatch, and
emailing a backup as an attachment.

Config backups are a flat asset type (no system/artwork association, like
movies) -- these tests mirror test_movies_transfer.py's structure, adapted
for the differences: identity is file_name (not a nested relative path),
integrity is a size check only (no fingerprint -- backups have none), and a
successful pull registers a brand-new local config_backups row instead of
just landing a file a poller will pick up later.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.device import config_backup
from app.storage import config_backup_store
from app.transfer.download_manager import DownloadManager
from app.transfer.peer_download import _download_config_backup_from_peer
from app.web import handlers_peer
from app.web import handlers_config_backup


def _settings(root: Path) -> Settings:
    with mock.patch.dict(
        "os.environ",
        {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "config-backup-test-device",
        },
        clear=True,
    ):
        return Settings.from_env()


class _FakeResponse:
    def __init__(self, data: bytes):
        self._chunks = [data, b""]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _size=-1):
        return self._chunks.pop(0)

    headers = {}


class DownloadConfigBackupFromPeerTests(unittest.TestCase):
    def test_happy_path_writes_prefixed_file_and_registers_local_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = _settings(root)
            pinned_cert = Path(tmp) / "peer-cert.pem"
            pinned_cert.write_text("peer-cert", encoding="utf-8")
            content = b"tarball-bytes" * 100
            peer = {"drone_id": "bff-drone-b", "name": "Living Room Drone", "reachable_url": "https://bff-drone-b:443"}

            requests = []

            def fake_urlopen(request, timeout=None, context=None):
                requests.append(request.full_url)
                return _FakeResponse(content)

            with mock.patch(
                "app.transfer.peer_download._peer_trust_cafile", return_value=pinned_cert
            ), mock.patch(
                "app.transfer.peer_download._drone_client_ssl_context", return_value=object()
            ), mock.patch(
                "app.transfer.peer_download.urlopen", side_effect=fake_urlopen
            ):
                result = _download_config_backup_from_peer(
                    settings, {}, peer, "drone-config-backup-20260101-000000.tar.gz",
                    expected_size=len(content),
                    backup_name="Before update",
                    backup_description="Just in case",
                    source_created_at="2026-01-01T00:00:00+00:00",
                )

            self.assertEqual(
                requests,
                ["https://bff-drone-b:443/v1/api/peer/config-backups/drone-config-backup-20260101-000000.tar.gz"],
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["asset_type"], "config_backups")
            self.assertEqual(result["bytes_transferred"], len(content))
            self.assertIsNotNone(result["config_backup_id"])

            # Prefixed with the source peer's drone_id to avoid ever colliding
            # with (or overwriting) a locally-built backup.
            written = config_backup.backups_directory(settings) / "bff-drone-b-drone-config-backup-20260101-000000.tar.gz"
            self.assertTrue(written.is_file())
            self.assertEqual(written.read_bytes(), content)

            row = config_backup_store.get(settings, result["config_backup_id"])
            self.assertEqual(row["status"], "complete")
            self.assertEqual(row["name"], "Before update")
            self.assertEqual(row["description"], "Just in case")
            self.assertEqual(row["source_drone_id"], "bff-drone-b")
            self.assertEqual(row["source_drone_name"], "Living Room Drone")
            self.assertEqual(row["source_created_at"], "2026-01-01T00:00:00+00:00")
            self.assertFalse(row["is_local"])

    def test_size_mismatch_raises_and_cleans_up_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = _settings(root)
            pinned_cert = Path(tmp) / "peer-cert.pem"
            pinned_cert.write_text("peer-cert", encoding="utf-8")
            peer = {"drone_id": "bff-drone-b", "reachable_url": "https://bff-drone-b:443"}
            with mock.patch(
                "app.transfer.peer_download._peer_trust_cafile", return_value=pinned_cert
            ), mock.patch(
                "app.transfer.peer_download._drone_client_ssl_context", return_value=object()
            ), mock.patch(
                "app.transfer.peer_download.urlopen", return_value=_FakeResponse(b"short")
            ):
                with self.assertRaises(RuntimeError):
                    _download_config_backup_from_peer(
                        settings, {}, peer, "drone-config-backup-20260101-000000.tar.gz",
                        expected_size=99999,
                    )
            target = config_backup.backups_directory(settings) / "bff-drone-b-drone-config-backup-20260101-000000.tar.gz"
            self.assertFalse(target.exists())
            self.assertFalse(target.with_name(target.name + ".part").exists())
            # A failed pull must never leave a dangling DB row either.
            self.assertEqual(config_backup_store.list_all(settings), [])

    def test_rejects_target_path_escaping_backups_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = _settings(root)
            peer = {"drone_id": "bff-drone-b", "reachable_url": "https://bff-drone-b:443"}
            with self.assertRaises(ValueError):
                _download_config_backup_from_peer(settings, {}, peer, "../../etc/passwd")


class _FakePeerHandler:
    """Minimal stand-in for RomRequestHandler's send/stream/log surface,
    mirroring the pattern in test_movies_transfer.py."""

    def __init__(self, settings: Settings, *, authorized: bool = True) -> None:
        self.settings = settings
        self._authorized = authorized
        self.response = None
        self.streamed = None

    def _peer_request_authorized(self) -> bool:
        return self._authorized

    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.response = (status_code, payload)

    def _stream_file(self, path, content_type, as_attachment=False, **kwargs) -> None:
        self.streamed = {"path": path, "content_type": content_type, "as_attachment": as_attachment}

    def log_error(self, *args, **kwargs) -> None:
        pass

    def log_message(self, *args, **kwargs) -> None:
        pass


def _peer_handler(settings: Settings, **kwargs) -> _FakePeerHandler:
    class Handler(_FakePeerHandler, handlers_peer.HandlersPeerMixin):
        pass

    return Handler(settings, **kwargs)


class HandlePeerConfigBackupDownloadTests(unittest.TestCase):
    def test_rejects_unauthorized_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            handler = _peer_handler(settings, authorized=False)
            handler._handle_peer_config_backup_download("a.tar.gz")
            self.assertIsNone(handler.response)
            self.assertIsNone(handler.streamed)

    def test_rejects_path_traversal_and_separators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            handler = _peer_handler(settings)
            handler._handle_peer_config_backup_download("../../etc/passwd")
            self.assertEqual(handler.response[0], 400)

    def test_404_when_not_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            handler = _peer_handler(settings)
            handler._handle_peer_config_backup_download("does-not-exist.tar.gz")
            self.assertEqual(handler.response[0], 404)

    def test_404_when_row_exists_but_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            config_backup_store.create_pending(settings, "still-building.tar.gz")
            handler = _peer_handler(settings)
            handler._handle_peer_config_backup_download("still-building.tar.gz")
            self.assertEqual(handler.response[0], 404)

    def test_streams_existing_complete_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            directory = config_backup.backups_directory(settings)
            target = directory / "a.tar.gz"
            target.write_bytes(b"tarball-bytes")
            row = config_backup_store.create_pending(settings, "a.tar.gz")
            config_backup_store.mark_complete(
                settings, row["id"], size_bytes=13, included_file_count=1, skipped_file_count=0, skipped_bytes=0
            )
            handler = _peer_handler(settings)
            handler._handle_peer_config_backup_download("a.tar.gz")
            self.assertIsNone(handler.response)
            self.assertEqual(handler.streamed["path"], target.resolve())
            self.assertTrue(handler.streamed["as_attachment"])


class CollectPeerInventoryConfigBackupsTests(unittest.TestCase):
    def test_only_complete_backups_are_listed_with_name_and_no_system(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            complete_row = config_backup_store.create_pending(settings, "done.tar.gz", name="Weekly", description="desc")
            config_backup_store.mark_complete(
                settings, complete_row["id"], size_bytes=100, included_file_count=5, skipped_file_count=0, skipped_bytes=0
            )
            config_backup_store.create_pending(settings, "still-building.tar.gz")  # status=creating, must not appear

            handler = _peer_handler(settings)
            payload = handler._collect_peer_inventory("config_backups", {})
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["asset_type"], "config_backups")
            self.assertEqual(len(payload["items"]), 1)
            item = payload["items"][0]
            self.assertEqual(item["name"], "Weekly")
            self.assertEqual(item["file_name"], "done.tar.gz")
            self.assertNotIn("system", item)


class EnqueueLocalConfigBackupAssetTests(unittest.TestCase):
    def test_enqueue_local_asset_dispatches_to_enqueue_config_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from app.web import handlers_network

            settings = _settings(Path(tmp) / "userdata")

            class Handler(handlers_network.HandlersNetworkMixin):
                def __init__(self, settings):
                    self.settings = settings

            handler = Handler(settings)
            manager = mock.create_autospec(DownloadManager, instance=True)
            manager.enqueue_config_backup.return_value = {"id": "job-1", "asset_type": "config_backups"}
            item = {
                "file_name": "done.tar.gz",
                "size_bytes": 4096,
                "name": "Weekly",
                "description": "desc",
                "created_at": "2026-01-01T00:00:00+00:00",
            }

            jobs = handler._enqueue_local_asset(manager, {}, {"drone_id": "peer-1"}, "config_backups", item)

            manager.enqueue_config_backup.assert_called_once_with(
                {}, {"drone_id": "peer-1"}, "done.tar.gz",
                expected_size=4096, backup_name="Weekly", backup_description="desc",
                source_created_at="2026-01-01T00:00:00+00:00", overwrite=False,
            )
            self.assertEqual(jobs, [{"id": "job-1", "asset_type": "config_backups"}])


class EmailBackupTests(unittest.TestCase):
    def test_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            row = config_backup_store.create_pending(settings, "a.tar.gz")
            config_backup_store.mark_complete(
                settings, row["id"], size_bytes=10, included_file_count=1, skipped_file_count=0, skipped_bytes=0
            )
            with mock.patch("app.device.config_backup._smtp.get_settings", return_value={"has_config": False}):
                result = config_backup.email_backup(settings, row["id"])
            self.assertEqual(result["status"], "not_configured")

    def test_too_large(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            row = config_backup_store.create_pending(settings, "a.tar.gz")
            oversized = config_backup.BACKUP_EMAIL_MAX_BYTES + 1
            config_backup_store.mark_complete(
                settings, row["id"], size_bytes=oversized, included_file_count=1, skipped_file_count=0, skipped_bytes=0
            )
            with mock.patch("app.device.config_backup._smtp.get_settings", return_value={"has_config": True}):
                result = config_backup.email_backup(settings, row["id"])
            self.assertEqual(result["status"], "too_large")
            self.assertEqual(result["size_bytes"], oversized)
            self.assertEqual(result["limit_bytes"], config_backup.BACKUP_EMAIL_MAX_BYTES)

    def test_not_found_for_incomplete_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            row = config_backup_store.create_pending(settings, "a.tar.gz")
            result = config_backup.email_backup(settings, row["id"])
            self.assertEqual(result["status"], "not_found")

    def test_queues_with_metadata_and_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = _settings(root)
            directory = config_backup.backups_directory(settings)
            (directory / "a.tar.gz").write_bytes(b"tarball-bytes")
            row = config_backup_store.create_pending(settings, "a.tar.gz", name="Weekly", description="desc")
            config_backup_store.mark_complete(
                settings, row["id"], size_bytes=13, included_file_count=3, skipped_file_count=0, skipped_bytes=0
            )
            calls = []

            def fake_queue(settings, **job):
                calls.append(job)
                return {"status": "queued", "job_id": 7, "queued_at": "2026-08-14T12:00:00+00:00"}

            with mock.patch("app.device.config_backup._smtp.get_settings", return_value={"has_config": True}), mock.patch(
                "app.device.config_backup._smtp.queue_mail", side_effect=fake_queue
            ), mock.patch(
                "app.device.config_backup._smtp.send_mail_with_attachment"
            ) as direct_send:
                result = config_backup.email_backup(settings, row["id"])

            direct_send.assert_not_called()
            self.assertEqual(result["status"], "queued")
            self.assertEqual(len(calls), 1)
            subject = calls[0]["subject"]
            body = calls[0]["body"]
            attachment_path = calls[0]["attachment_path"]
            attachment_filename = calls[0]["attachment_filename"]
            self.assertIn("Weekly", subject)
            self.assertIn("Weekly", body)
            self.assertIn("desc", body)
            self.assertIn("Batocera version", body)
            self.assertEqual(attachment_filename, "a.tar.gz")
            self.assertEqual(attachment_path, directory / "a.tar.gz")

    def test_queue_result_is_returned_without_opening_smtp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "userdata"
            settings = _settings(root)
            directory = config_backup.backups_directory(settings)
            (directory / "a.tar.gz").write_bytes(b"tarball-bytes")
            row = config_backup_store.create_pending(settings, "a.tar.gz")
            config_backup_store.mark_complete(
                settings, row["id"], size_bytes=13, included_file_count=1, skipped_file_count=0, skipped_bytes=0
            )
            with mock.patch("app.device.config_backup._smtp.get_settings", return_value={"has_config": True}), mock.patch(
                "app.device.config_backup._smtp.queue_mail",
                return_value={"status": "queued", "job_id": 9, "queued_at": "2026-08-14T12:00:00+00:00"},
            ), mock.patch("app.device.config_backup._smtp.send_mail_with_attachment") as direct_send:
                result = config_backup.email_backup(settings, row["id"])
            direct_send.assert_not_called()
            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["job_id"], 9)


class EmailBackupHandlerTests(unittest.TestCase):
    class Handler(handlers_config_backup.HandlersConfigBackupMixin):
        def __init__(self, settings):
            self.settings = settings
            self.response = None

        def _send_json(self, status_code, payload):
            self.response = (status_code, payload)

    def test_queued_backup_email_returns_202(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            handler = self.Handler(settings)
            with mock.patch.object(
                config_backup,
                "email_backup",
                return_value={"status": "queued", "job_id": 11},
            ):
                handler._handle_admin_config_backup_email("5")
            self.assertEqual(handler.response, (202, {"status": "queued", "job_id": 11}))

    def test_validation_outcome_remains_synchronous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            handler = self.Handler(settings)
            with mock.patch.object(
                config_backup,
                "email_backup",
                return_value={"status": "not_configured"},
            ):
                handler._handle_admin_config_backup_email("5")
            self.assertEqual(handler.response, (200, {"status": "not_configured"}))


if __name__ == "__main__":
    unittest.main()
