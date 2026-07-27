"""RomRequestHandler torrent-admin handlers, as a mixin.

The admin torrent endpoints: snapshot the watched-folder torrent queue, save
its settings, install aria2c, upload .torrent files (one or many per request),
force-start/cancel/delete a torrent, and the directory-picker listing.
Composed onto ``RomRequestHandler``.
"""

TORRENT_UPLOAD_MAX_BODY_BYTES = 64 * 1024 * 1024


def _parse_multipart_files(raw_body: bytes, boundary: str) -> list:
    """Extract every file part from a multipart/form-data body as (name, bytes).

    Byte-exact for binary payloads: only the single CRLF that belongs to the
    multipart framing is removed, never trailing bytes of the file itself.
    """
    delimiter = b"--" + boundary.encode("utf-8", errors="replace")
    files = []
    for chunk in raw_body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break  # closing delimiter
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        header_end = chunk.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        headers = chunk[:header_end].decode("utf-8", errors="replace")
        body = chunk[header_end + 4 :]
        if body.endswith(b"\r\n"):
            body = body[:-2]
        disposition = next(
            (line for line in headers.split("\r\n") if line.lower().startswith("content-disposition")),
            "",
        )
        if ' filename="' not in disposition:
            continue
        filename = disposition.split(' filename="')[1].split('"')[0]
        if filename:
            files.append((filename, body))
    return files


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
        boundary = content_type.split("boundary=")[1].strip() if "boundary=" in content_type else None
        if not boundary:
            raise ValueError("boundary not found in content-type")
        boundary = boundary.strip('"').strip("'")
        files = _parse_multipart_files(self.rfile.read(content_length), boundary)
        if not files:
            raise ValueError("no torrent files in upload")
        result = manager.save_uploaded_torrents(files)
        self._send_json(200 if result.get("saved") else 400, result)

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
