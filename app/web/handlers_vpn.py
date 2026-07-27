"""RomRequestHandler VPN-admin handlers, as a mixin.

The admin OpenVPN endpoints: status snapshot, upload a provider's .ovpn,
save credentials, connect/disconnect, on-demand public-IP verification,
the auto-start-on-boot toggle, and the log (view + download). Composed onto
``RomRequestHandler``. See ``device/vpn_manager.py`` for the actual OpenVPN
process/config management this delegates to.
"""

try:
    from ..common.multipart import boundary_from_content_type as _boundary_from_content_type
    from ..common.multipart import parse_multipart_files as _parse_multipart_files
    from ..device import vpn_manager as _vpn
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.multipart import boundary_from_content_type as _boundary_from_content_type  # type: ignore
    from common.multipart import parse_multipart_files as _parse_multipart_files  # type: ignore
    from device import vpn_manager as _vpn  # type: ignore

VPN_UPLOAD_MAX_BODY_BYTES = 6 * 1024 * 1024


class HandlersVpnMixin:
    def _handle_admin_vpn_status(self) -> None:
        self._send_json(200, _vpn.status(self.settings))

    def _handle_admin_vpn_upload(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("multipart/form-data expected")
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        if content_length <= 0 or content_length > VPN_UPLOAD_MAX_BODY_BYTES:
            raise ValueError("invalid content size")
        boundary = _boundary_from_content_type(content_type)
        files = _parse_multipart_files(self.rfile.read(content_length), boundary)
        if not files:
            raise ValueError("no .ovpn file in upload")
        filename, payload = files[0]
        result = _vpn.save_uploaded_config(self.settings, filename, payload)
        self._send_json(200, result)

    def _handle_admin_vpn_credentials(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        result = _vpn.save_credentials(self.settings, payload.get("username"), payload.get("password"))
        self._send_json(200, result)

    def _handle_admin_vpn_connect(self) -> None:
        result = _vpn.connect(self.settings)
        status_code = 400 if result.get("status") == "error" else 200
        self._send_json(status_code, result)

    def _handle_admin_vpn_disconnect(self) -> None:
        result = _vpn.disconnect(self.settings)
        status_code = 500 if result.get("status") == "error" else 200
        self._send_json(status_code, result)

    def _handle_admin_vpn_verify_ip(self) -> None:
        result = _vpn.check_public_ip()
        self._send_json(200 if "ip" in result else 502, result)

    def _handle_admin_vpn_auto_start(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        result = _vpn.set_auto_start(self.settings, bool(payload.get("enabled")))
        self._send_json(200, result)

    def _handle_admin_vpn_log_download(self) -> None:
        path = _vpn.log_path(self.settings)
        if not path.is_file():
            raise FileNotFoundError()
        self._stream_file(path, "text/plain", as_attachment=True)
