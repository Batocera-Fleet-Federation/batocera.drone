"""RomRequestHandler VPN-admin handlers, as a mixin.

The admin OpenVPN endpoints: status snapshot, upload a provider's .ovpn,
save credentials, connect/disconnect, on-demand public-IP verification, the
P2P sharing toggle/pull-from-peer, and the log (view + download). Composed
onto ``RomRequestHandler``. See ``device/vpn_manager.py`` for the actual
OpenVPN process/config management this delegates to -- including
``maybe_auto_connect()``, which connects on Drone startup automatically
whenever a config is ready, with no separate admin action here for it.
"""

from urllib.error import HTTPError

try:
    from ..common.multipart import boundary_from_content_type as _boundary_from_content_type
    from ..common.multipart import parse_multipart_files as _parse_multipart_files
    from ..device import vpn_manager as _vpn
    from ..transfer import local_network as _local_network
    from ..transfer.peer_connectivity import _peer_get_json_for_peer
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.multipart import boundary_from_content_type as _boundary_from_content_type  # type: ignore
    from common.multipart import parse_multipart_files as _parse_multipart_files  # type: ignore
    from device import vpn_manager as _vpn  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.peer_connectivity import _peer_get_json_for_peer  # type: ignore

VPN_UPLOAD_MAX_BODY_BYTES = 6 * 1024 * 1024
VPN_PEER_PULL_ENDPOINT = "/v1/api/peer/vpn/config"


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

    def _handle_admin_vpn_sharing(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        result = _vpn.set_sharing_enabled(self.settings, bool(payload.get("enabled")))
        self._send_json(200, result)

    def _handle_admin_vpn_self_heal(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        result = _vpn.set_self_heal_enabled(self.settings, bool(payload.get("enabled")))
        self._send_json(200, result)

    def _handle_admin_vpn_pull_from_peer(self, payload: dict) -> None:
        """Pull VPN config (+ credentials) from a paired peer and adopt it locally.

        Reuses the same cert-pinned mTLS client (_peer_get_json_for_peer) that
        backs every other peer request -- this is a small one-shot JSON call
        against a peer this drone already trusts, not the big-file download
        queue used for ROM/BIOS/saves/movies, so no progress/resume machinery
        is needed here.
        """
        payload = payload if isinstance(payload, dict) else {}
        peer_id = str(payload.get("peer_id") or "").strip()
        if not peer_id:
            raise ValueError("peer_id is required")
        peer = _local_network.get_paired_peer(self.settings, peer_id)
        if not peer:
            self._send_json(404, {"error": "That drone is not a paired peer."})
            return
        try:
            remote_payload, _address = _peer_get_json_for_peer(peer, VPN_PEER_PULL_ENDPOINT, self.settings, peer_id=peer_id)
        except HTTPError as error:
            if error.code == 404:
                self._send_json(404, {"error": "That drone has VPN sharing turned off, or has no configuration uploaded yet."})
            else:
                self._send_json(502, {"error": f"That drone rejected the request (HTTP {error.code})."})
            return
        except Exception as error:
            self._send_json(502, {"error": f"Could not reach that drone: {error}"})
            return
        peer_name = str(peer.get("name") or peer.get("hostname") or peer_id)
        result = _vpn.import_from_peer(self.settings, remote_payload, source_peer_id=peer_id, source_peer_name=peer_name)
        self._send_json(200, result)

    def _handle_admin_vpn_log_download(self) -> None:
        path = _vpn.log_path(self.settings)
        if not path.is_file():
            raise FileNotFoundError()
        self._stream_file(path, "text/plain", as_attachment=True)

    def _handle_admin_vpn_import_folder_list(self) -> None:
        self._send_json(200, _vpn.list_import_files(self.settings))

    def _handle_admin_vpn_import_folder_apply(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        filename = str(payload.get("filename") or "").strip()
        if not filename:
            raise ValueError("filename is required")
        # api_routes.py's generic exception-to-status mapping only catches
        # FileNotFoundError -> 404 on the GET dispatch path, not POST (POST
        # only auto-maps ValueError -> 400) -- confirmed reading both
        # try/except blocks directly. _handle_admin_vpn_pull_from_peer above
        # already follows this same explicit-404 pattern for the same
        # reason; letting import_from_folder's FileNotFoundError propagate
        # bare here would produce a wrong 500, not a 404.
        try:
            result = _vpn.import_from_folder(self.settings, filename)
        except FileNotFoundError as error:
            self._send_json(404, {"error": str(error) or "File not found in the drop folder"})
            return
        self._send_json(200, result)
