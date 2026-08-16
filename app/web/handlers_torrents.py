"""RomRequestHandler torrent-admin handlers, as a mixin.

The admin torrent endpoints: snapshot the watched-folder torrent queue, save
its settings, install aria2c, upload .torrent files (one or many per request),
force-start/cancel/delete a torrent, and the directory-picker listing.
Composed onto ``RomRequestHandler``.
"""

try:
    from ..common.multipart import boundary_from_content_type as _boundary_from_content_type
    from ..common.multipart import parse_multipart_files as _parse_multipart_files
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.multipart import boundary_from_content_type as _boundary_from_content_type  # type: ignore
    from common.multipart import parse_multipart_files as _parse_multipart_files  # type: ignore

TORRENT_UPLOAD_MAX_BODY_BYTES = 64 * 1024 * 1024


def _get_torrent_manager():
    """Delegate to the drone_api singleton accessor (lazy to avoid a cycle)."""
    try:
        from ..drone_api import _get_torrent_manager as _impl
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import _get_torrent_manager as _impl  # type: ignore
    return _impl()


class HandlersTorrentsMixin:
    def _handle_admin_torrents(self) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        self._send_json(200, manager.snapshot())

    def _handle_admin_torrents_settings_update(self, payload: dict) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        # The watched torrent folder is no longer user-configurable -- always
        # the install-root default (see default_torrent_directory()). Drop
        # any "directory" the client sends so a stale/crafted payload can't
        # override it; TorrentManager.update_settings() itself stays generic
        # (tests and other internal callers still point it at an arbitrary
        # directory directly).
        payload = {key: value for key, value in (payload or {}).items() if key != "directory"}
        self._send_json(200, {"settings": manager.update_settings(payload)})

    def _handle_admin_torrents_browse(self, raw_path: str) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        self._send_json(200, manager.browse_directories(raw_path))

    def _handle_admin_torrents_upload(self) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("multipart/form-data expected")
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        if content_length <= 0 or content_length > TORRENT_UPLOAD_MAX_BODY_BYTES:
            raise ValueError("invalid content size")
        boundary = _boundary_from_content_type(content_type)
        files = _parse_multipart_files(self.rfile.read(content_length), boundary)
        if not files:
            raise ValueError("no torrent files in upload")
        result = manager.save_uploaded_torrents(files)
        self._send_json(200 if result.get("saved") else 400, result)

    def _handle_admin_torrents_add_magnet(self, payload: dict) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        payload = payload if isinstance(payload, dict) else {}
        magnet_uri = str(payload.get("magnet_uri") or payload.get("uri") or "")
        try:
            result = manager.add_magnet(magnet_uri)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(200, result)

    def _handle_admin_torrents_aria2_install(self) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        self._send_json(200, manager.install_aria2())

    def _handle_admin_torrent_force_start(self, torrent_id: str) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        result = manager.force_start(torrent_id)
        status_code = 404 if result.get("status") == "not_found" else 409 if result.get("status") == "not_applicable" else 200
        self._send_json(status_code, result)

    def _handle_admin_torrent_cancel(self, torrent_id: str) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        result = manager.cancel(torrent_id)
        status_code = 404 if result.get("status") == "not_found" else 409 if result.get("status") == "not_cancelable" else 200
        self._send_json(status_code, result)

    def _handle_admin_torrent_delete(self, torrent_id: str) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        result = manager.delete(torrent_id)
        status_code = 404 if result.get("status") == "not_found" else 200
        self._send_json(status_code, result)

    def _handle_admin_torrent_files(self, torrent_id: str) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        result = manager.list_files(torrent_id)
        status_code = 404 if result.get("status") == "not_found" else 409 if result.get("status") == "not_applicable" else 200
        self._send_json(status_code, result)

    def _handle_admin_torrent_move(self, torrent_id: str, payload: dict) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        payload = payload if isinstance(payload, dict) else {}
        result = manager.move_files(
            torrent_id,
            payload.get("files"),
            str(payload.get("destination") or ""),
            cleanup=bool(payload.get("cleanup")),
            preserve_structure=bool(payload.get("preserve_structure")),
        )
        status_map = {
            "not_found": 404,
            "not_applicable": 409,
            "no_files_selected": 400,
            "invalid_destination": 400,
            "already_in_progress": 409,
        }
        # "queued" (the normal success path -- move_files() now only
        # enqueues a background move_job, see torrent_manager.py) falls
        # through to the 200 default.
        self._send_json(status_map.get(result.get("status"), 200), result)

    def _handle_admin_torrents_pause(self) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        self._send_json(200, manager.pause())

    def _handle_admin_torrents_resume(self) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        self._send_json(200, manager.resume())

    def _handle_admin_torrents_clear(self, payload: dict) -> None:
        manager = _get_torrent_manager()
        if manager is None:
            self._send_json(503, {"error": "torrent manager unavailable"})
            return
        result = manager.clear(payload)
        status_code = 400 if result.get("status") == "no_action_selected" else 200
        self._send_json(status_code, result)
