"""RomRequestHandler config-backup admin handlers, as a mixin.

List/create/download/delete for the "Backups" admin tile -- bundles
Batocera + emulator settings into a downloadable tarball. Thin dispatch;
all real logic lives in ``device/config_backup.py`` (tarball build) and
``storage/config_backup_store.py`` (metadata). Composed onto
``RomRequestHandler``.
"""

try:
    from ..device import config_backup as _config_backup
    from ..storage import config_backup_store as _config_backup_store
except ImportError:  # pragma: no cover - direct script execution fallback
    from device import config_backup as _config_backup  # type: ignore
    from storage import config_backup_store as _config_backup_store  # type: ignore


class HandlersConfigBackupMixin:
    def _handle_admin_config_backups_list(self) -> None:
        self._send_json(200, {"backups": _config_backup_store.list_all(self.settings)})

    def _handle_admin_config_backups_create(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        result = _config_backup.create_backup(
            self.settings,
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
        )
        status_code = 409 if result.get("status") == "already_creating" else 200
        self._send_json(status_code, result)

    def _handle_admin_config_backup_download(self, raw_backup_id: str) -> None:
        try:
            backup_id = int(raw_backup_id)
        except ValueError:
            raise FileNotFoundError()
        row = _config_backup_store.get(self.settings, backup_id)
        if row is None or row.get("status") != "complete":
            raise FileNotFoundError()
        file_path = _config_backup.backups_directory(self.settings) / row["file_name"]
        if not file_path.is_file():
            raise FileNotFoundError()
        self._stream_file(file_path, "application/gzip", as_attachment=True)

    def _handle_admin_config_backup_delete(self, raw_backup_id: str) -> None:
        try:
            backup_id = int(raw_backup_id)
        except ValueError:
            self._send_json(404, {"status": "not_found"})
            return
        result = _config_backup.delete_backup(self.settings, backup_id)
        status_code = 404 if result.get("status") == "not_found" else 200
        self._send_json(status_code, result)

    def _handle_admin_config_backup_email(self, raw_backup_id: str) -> None:
        try:
            backup_id = int(raw_backup_id)
        except ValueError:
            self._send_json(404, {"status": "not_found"})
            return
        result = _config_backup.email_backup(self.settings, backup_id)
        # "not_configured"/"too_large"/"error" are expected, meaningful
        # outcomes the frontend branches on directly (e.g. a popup explaining
        # how to set up email) -- they stay 200 so apiPost() doesn't collapse
        # them into a generic thrown Error and lose that distinction. Only a
        # genuinely unexpected case (the backup vanished between page load
        # and the click) gets a real 404.
        status_code = 404 if result.get("status") == "not_found" else 200
        self._send_json(status_code, result)
